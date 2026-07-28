import threading
import subprocess
import logging
import uuid
import tempfile
import os
import re
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from app import db

logger = logging.getLogger('Oraculo')

# ==========================================================================
# FAIXAS DE PROGRESSO POR FASE (piso, teto)
# --------------------------------------------------------------------------
# Garantem que a barra SEMPRE avance conforme a fase muda, mesmo quando o
# Nmap não emite a linha "About X% done" (o que acontece muito na fase de
# scripts de vulnerabilidade / NSE, que costuma ser a mais demorada).
# ==========================================================================
PHASES = {
    "Inicializando...":    (1, 5),
    "Conectividade":       (5, 12),
    "Varredura TCP":       (12, 50),
    "Análise de Serviços": (50, 62),
    "Detecção de SO":      (62, 68),
    "Vulnerabilidades":    (68, 92),
}

def init_scan_db():
    # O schema (tabela 'scans' e colunas owner/session_id) já é criado de forma
    # portável (SQLite/PostgreSQL) por db.init_schema(). Mantido como função para
    # não quebrar quem já chama init_scan_db() em outros pontos do código.
    db.init_schema()
init_scan_db()

def parse_nmap_xml_file(file_path):
    try:
        tree = ET.parse(file_path); hosts = []
        for host in tree.getroot().findall("host"):
            addr = host.find("address"); ip = addr.get("addr") if addr is not None else "Desconhecido"
            ports = []
            for port in host.findall(".//port"):
                state = port.find("state").get("state")
                if state != "open": continue
                port_id = port.get("portid"); proto = port.get("protocol")
                service = port.find("service"); svc_name = service.get("name") if service is not None else "unknown"
                version = service.get("version") if service is not None else ""
                vulns = [{"id": s.get("id", ""), "output": s.get("output", "")} for s in port.findall("script")]
                ports.append({"id": f"{port_id}/{proto}", "service": svc_name, "version": version, "vulns": vulns})
            hosts.append({"ip": ip, "ports": ports})
        return hosts
    except Exception:
        return []

# Extrai de forma determinística as CVEs (e o CVSS quando presente) de dentro
# da saída dos scripts do Nmap. Assim garantimos as referências mesmo que a IA
# esqueça de citar alguma. Retorna [(cve, score|None), ...] ordenado por severidade.
CVE_RE = re.compile(r"(CVE-\d{4}-\d{4,7})(?:\s*[:\-]?\s*(\d{1,2}\.\d))?", re.IGNORECASE)

def extract_cves(scan_data):
    encontrados = {}
    for host in scan_data:
        for port in host.get("ports", []):
            for v in port.get("vulns", []):
                texto = f"{v.get('id', '')} {v.get('output', '')}"
                for m in CVE_RE.finditer(texto):
                    cve = m.group(1).upper()
                    score = m.group(2)
                    if cve not in encontrados or (score and not encontrados[cve]):
                        encontrados[cve] = score
    def _sev(item):
        _cve, sc = item
        return (-(float(sc) if sc else -1.0), _cve)
    return sorted(encontrados.items(), key=_sev)

def _severidade_cvss(score):
    """Traduz um score CVSS numérico na faixa de severidade textual (padrão
    CVSS v3: 0.1-3.9 Baixa, 4.0-6.9 Média, 7.0-8.9 Alta, 9.0-10.0 Crítica).
    Sem score (None), devolve 'Indeterminada' — comum quando o script do Nmap
    reporta a CVE mas não o vetor completo."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "Indeterminada"
    if s >= 9.0:
        return "Crítica"
    if s >= 7.0:
        return "Alta"
    if s >= 4.0:
        return "Média"
    if s > 0.0:
        return "Baixa"
    return "Informativa"

def update_scan_progress(scan_id, progress, stage, status="running", result_json=None):
    try:
        if result_json is not None:
            db.execute(
                "UPDATE scans SET progress=?, stage=?, status=?, result_json=? WHERE id=?",
                (progress, stage, status, json.dumps(result_json), scan_id)
            )
        else:
            db.execute(
                "UPDATE scans SET progress=?, stage=?, status=? WHERE id=?",
                (progress, stage, status, scan_id)
            )
    except Exception as e:
        # Antes era 'pass' silencioso: um lock aqui derrubava a barra sem deixar rastro.
        logger.warning(f"[SecOps] Falha ao atualizar progresso do scan {scan_id}: {e}")

def start_scan_thread(target, username, session_id=None):
    # --- Defesa em profundidade: reforça a regra admin-only na própria camada
    #     do SecOps, independente de quem chamou. Fonte única da verdade em ai_engine. ---
    try:
        from app.services.ai_engine import perfil_pode_escanear, get_user_profile
        perfil = get_user_profile(username)
        if not perfil_pode_escanear(perfil):
            logger.warning(f"[SecOps BLOCKED] start_scan_thread recusado para '{username}' (Perfil: {perfil}).")
            return None
    except Exception as e:
        # Em caso de dúvida sobre a autorização, NÃO executa (fail-closed).
        logger.error(f"[SecOps] Falha ao validar autorização de '{username}': {e}. Scan abortado.")
        return None

    scan_id = str(uuid.uuid4())

    db.execute(
        "INSERT INTO scans (id, target, progress, stage, status, owner, session_id) VALUES (?, ?, 0, 'Inicializando...', 'running', ?, ?)",
        (scan_id, target, username, session_id)
    )

    def run_scan():
        from app.services.ai_engine import exec_chat_with_oracle

        logger.info(f"[SecOps] Varredura Progressiva em {target}")

        # ----- Estado de progresso compartilhado (thread-safe) -----
        lock = threading.Lock()
        state = {"progress": 1.0, "stage": "Inicializando...", "ceiling": 5.0}
        stop_event = threading.Event()

        def push(raw_pct=None, stage=None):
            """Atualiza o progresso de forma MONOTÔNICA (nunca volta atrás) e
            escala o '% da fase' do Nmap dentro da faixa [piso, teto] da fase atual."""
            with lock:
                if stage and stage in PHASES:
                    floor, ceil = PHASES[stage]
                    state["stage"] = stage
                    state["ceiling"] = ceil
                    if state["progress"] < floor:
                        state["progress"] = floor
                floor, ceil = PHASES.get(state["stage"], (state["progress"], state["ceiling"]))
                if raw_pct is not None:
                    scaled = floor + (max(0.0, min(100.0, raw_pct)) / 100.0) * (ceil - floor)
                    if scaled > state["progress"]:
                        state["progress"] = min(scaled, ceil)
                p, s = round(state["progress"], 1), state["stage"]
            update_scan_progress(scan_id, p, s)

        def heartbeat():
            """Faz a barra 'respirar': avança devagar rumo ao teto da fase para
            nunca parecer travada em fases longas (ex.: scripts de vuln/NSE),
            mesmo que o Nmap fique minutos sem imprimir '% done'."""
            while not stop_event.wait(4):
                with lock:
                    gap = state["ceiling"] - state["progress"]
                    if gap > 0.5:
                        state["progress"] += max(0.3, gap * 0.06)
                        p, s = round(state["progress"], 1), state["stage"]
                    else:
                        p = None
                if p is not None:
                    update_scan_progress(scan_id, p, s)

        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()

        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as temp_xml:
            xml_path = temp_xml.name

        # --stats-every + -v forçam o Nmap a emitir status periódico.
        comando = ["nmap", "-Pn", "-A", "--script", "vuln,smb-os-discovery,http-title",
                   "-T4", "--stats-every", "2s", "-v", "-oX", xml_path, target]

        resultado_final = ""
        final_status = "error"
        result_payload = None

        try:
            process = subprocess.Popen(
                comando, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, bufsize=1
            )

            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if not line:
                    continue
                line = line.strip()

                stage = None
                if "Ping Scan" in line:
                    stage = "Conectividade"
                elif "SYN Stealth" in line or "Connect Scan" in line or "Initiating SYN" in line or "Initiating Connect" in line:
                    stage = "Varredura TCP"
                elif "Service scan" in line or "Initiating Service" in line:
                    stage = "Análise de Serviços"
                elif "OS detection" in line or "Initiating OS" in line:
                    stage = "Detecção de SO"
                elif "NSE" in line or "Script scan" in line or "Script Scan" in line:
                    stage = "Vulnerabilidades"

                raw_pct = None
                m = re.search(r"About\s+(\d+(?:\.\d+)?)%", line)
                if m:
                    raw_pct = float(m.group(1))

                if stage or raw_pct is not None:
                    push(raw_pct=raw_pct, stage=stage)

            process.wait()

            if os.path.exists(xml_path) and os.path.getsize(xml_path) > 0:
                push(stage="Vulnerabilidades", raw_pct=100)  # empurra para o teto da última fase
                update_scan_progress(scan_id, 95, "Gerando relatório IA...")
                scan_data = parse_nmap_xml_file(xml_path)

                if scan_data:
                    cves = extract_cves(scan_data)
                    lista_cves = ", ".join(cve for cve, _ in cves) if cves else "nenhuma CVE explícita identificada"

                    prompt = (
                        f"Você é um consultor de segurança ofensiva sênior (OSCP/OSEP/Red Team) redigindo um "
                        f"relatório técnico-executivo formal para a diretoria e para a equipe técnica de um "
                        f"cliente corporativo. Idioma: Português do Brasil. Formato: Markdown, tom profissional, "
                        f"objetivo e sem sensacionalismo. Seja DETALHISTA e substancial: prefira uma análise rica "
                        f"e aprofundada a um laudo enxuto — este relatório será entregue ao cliente como documento "
                        f"final.\n\n"
                        f"ALVO AUDITADO: {target}\n"
                        f"CVEs DETECTADAS PELO SCANNER (use APENAS estas, não invente outras): {lista_cves}\n\n"
                        f"Estruture EXATAMENTE nesta ordem, usando cabeçalhos '##':\n\n"
                        f"## 1. Sumário Executivo\n"
                        f"- Abra com a linha **Risco Geral:** seguida de CRÍTICO, ALTO, MÉDIO ou BAIXO.\n"
                        f"- Escreva 3-5 frases de contexto voltadas ao IMPACTO DE NEGÓCIO (exposição, continuidade "
                        f"operacional, confidencialidade de dados, conformidade/LGPD quando fizer sentido).\n"
                        f"- Inclua um bullet **Principais Achados:** com 2-4 pontos-chave resumidos.\n"
                        f"- Inclua um bullet **Postura de Segurança:** com uma leitura honesta do que já está bem "
                        f"configurado no alvo (ex.: redirecionamento HTTP→HTTPS, ausência de serviços legados).\n\n"
                        f"## 2. Superfície de Ataque\n"
                        f"- Para CADA porta/serviço aberto, crie um bullet de nível 1 no formato "
                        f"**Porta X/tcp (SERVIÇO)** e, abaixo dele, sub-bullets recuados com: **Versão** detectada, "
                        f"**Função** do serviço, **Exposição** (interna/externa e o que um atacante enxerga), e "
                        f"**Risco** associado com justificativa técnica.\n"
                        f"- Comente banners, títulos HTTP, certificados e cabeçalhos relevantes quando presentes nos dados.\n\n"
                        f"## 3. Análise de Vulnerabilidades\n"
                        f"- Para CADA CVE relevante crie um bullet **CVE-AAAA-NNNN** (identificador em TEXTO PURO, "
                        f"NÃO crie hyperlink) e, recuados abaixo, sub-bullets com: **Severidade** (Crítica/Alta/Média/"
                        f"Baixa) e **CVSS** quando houver; **Descrição** técnica; **Vetor de Exploração** (como seria "
                        f"abusada na prática); **Impacto Potencial** ao negócio; e **Evidência** observada no scan.\n"
                        f"- Se NENHUMA CVE foi identificada, NÃO deixe a seção vazia: explique o que foi testado "
                        f"(CSRF, XSS refletido/armazenado/DOM, injeções comuns), reforce que ausência de CVE não é "
                        f"ausência de risco, e liste hipóteses de vulnerabilidades de camada de aplicação (L7) que um "
                        f"scanner automático não detecta e exigiriam teste manual/autenticado.\n\n"
                        f"## 4. Plano de Remediação Priorizado\n"
                        f"- Liste ações concretas em ordem de prioridade. Cada ação é um bullet de nível 1 com "
                        f"sub-bullets: **Prioridade** (P1/P2/P3), **Esforço** (Baixo/Médio/Alto), **Prazo** "
                        f"(Imediato / 30 dias / 90 dias) e **Benefício** esperado.\n"
                        f"- Inclua pelo menos hardening de cabeçalhos HTTP de segurança (HSTS, CSP, X-Frame-Options), "
                        f"gestão de versões/patch, TLS e monitoramento contínuo, mesmo em cenário de risco baixo.\n\n"
                        f"## 5. Recomendações Estratégicas\n"
                        f"- 3-5 recomendações de médio/longo prazo para elevar a maturidade de segurança "
                        f"(ex.: WAF, testes de intrusão periódicos, pipeline de varredura contínua, threat intel).\n\n"
                        f"## 6. Observações e Limitações\n"
                        f"- Ressalvas do scan (detecção sem autenticação, possíveis falsos positivos/negativos, "
                        f"escopo limitado à superfície exposta, recomendação de pentest manual como complemento).\n\n"
                        f"REGRAS: não use tabelas Markdown (use listas com marcadores '- ' e sub-listas recuadas com "
                        f"4 espaços; rótulos em **negrito**). Não invente CVEs fora da lista fornecida. Não invente "
                        f"portas/serviços que não estejam nos dados brutos. Seja técnico, direto e rico em detalhes.\n\n"
                        f"DADOS BRUTOS DO NMAP (JSON): {json.dumps(scan_data)}"
                    )
                    # Teto de tokens elevado SÓ para o laudo (o padrão de 2048 truncava o
                    # relatório no meio da seção 4, fazendo sumir remediação/observações).
                    ai_analysis = exec_chat_with_oracle(prompt, username, max_tokens=4096)

                    # Bloco de referências construído pelo código: garante que as CVEs
                    # apareçam mesmo que a IA esqueça alguma. Os IDs em texto puro são
                    # transformados em links clicáveis no chat e no relatório (via render).
                    report_md = ai_analysis
                    if cves:
                        linhas = "\n".join(
                            f"- **{cve}** — Severidade {_severidade_cvss(score)}"
                            + (f" (CVSS {score})" if score else " (CVSS não informado)")
                            + " — detalhes técnicos no NVD."
                            for cve, score in cves
                        )
                        report_md += (
                            "\n\n## 7. Referências (CVEs Identificadas)\n"
                            "As CVEs abaixo são convertidas em links clicáveis para o NVD (National "
                            "Vulnerability Database) no relatório e no chat:\n"
                            f"{linhas}"
                        )

                    resultado_final = f"🛡️ **Nmap scan report para {target}:**\n\n{report_md}\n\n[BOTÃO_DOWNLOAD:{scan_id}]"
                    result_payload = {"scan_data": scan_data, "ai_analysis": report_md}
                    final_status = "finished"

                    # Registra o relatório SecOps no índice unificado (tabela
                    # reports) para aparecer no painel Admin > Relatórios junto
                    # dos FinOps. Só metadado/ponteiro — o conteúdo continua em
                    # scans.result_json. Tolerante a falha: não pode derrubar o scan.
                    try:
                        from app.services import report_store
                        report_store.registrar_secops(scan_id, target=target, criado_por=username)
                    except Exception as e:
                        logger.error(f"[SecOps] Falha ao indexar relatório {scan_id}: {e}")
                else:
                    resultado_final = f"🛡️ **Nmap scan report:**\nNenhum host ativo detectado em `{target}`."
                    final_status = "error"
            else:
                resultado_final = "🛡️ **Nmap scan report:**\nFalha ao executar a varredura (o Nmap não retornou resultado)."
                final_status = "error"

        except FileNotFoundError:
            resultado_final = "🛡️ **Nmap scan report:**\nErro: o binário `nmap` não foi encontrado no servidor."
            final_status = "error"
        except Exception as e:
            logger.error(f"[SecOps] Erro no scan {scan_id}: {e}")
            resultado_final = f"🛡️ **Nmap scan report:**\nErro crítico: {str(e)}"
            final_status = "error"
        finally:
            # Para o heartbeat ANTES de gravar o estado final; senão ele poderia
            # sobrescrever o status 'finished' de volta para 'running'.
            stop_event.set()
            hb.join(timeout=6)
            if os.path.exists(xml_path):
                try:
                    os.remove(xml_path)
                except Exception:
                    pass

        # ================== ENTREGA NO CHAT (A ORDEM IMPORTA) ==================
        # 1) Primeiro grava a mensagem no histórico do usuário.
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        try:
            db.execute(
                "INSERT INTO chat_history (username, sender, content, is_html, timestamp, session_id) VALUES (?, 'bot', ?, 0, ?, ?)",
                (username, resultado_final, now_str, session_id)
            )
        except Exception as e:
            logger.error(f"[SecOps] Erro ao gravar relatório no chat: {e}")

        # 2) SÓ DEPOIS marca o scan como concluído (o que o remove de /api/active_scans).
        #    Assim, quando o frontend perceber o fim e recarregar o histórico,
        #    a mensagem JÁ estará garantidamente gravada. Isso elimina a corrida
        #    que fazia o relatório "sumir" e nunca aparecer no chat.
        update_scan_progress(scan_id, 100, "Concluído", final_status, result_payload)

    threading.Thread(target=run_scan, daemon=True).start()
    return scan_id
