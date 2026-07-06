import threading
import subprocess
import sqlite3
import logging
import uuid
import tempfile
import os
import re
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from config import DB_PATH

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
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                target TEXT,
                progress REAL DEFAULT 0,
                stage TEXT,
                status TEXT,
                result_json TEXT,
                owner TEXT
            )
        ''')
        # Garante colunas novas mesmo em bancos criados por versões antigas.
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(scans)").fetchall()]
            if "owner" not in cols:
                conn.execute("ALTER TABLE scans ADD COLUMN owner TEXT")
            if "session_id" not in cols:
                conn.execute("ALTER TABLE scans ADD COLUMN session_id TEXT")
        except Exception:
            pass
        # WAL + busy_timeout reduzem drasticamente os erros "database is locked"
        # quando o thread do scan e as requisições web tocam o SQLite ao mesmo tempo.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
        except Exception:
            pass
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

def update_scan_progress(scan_id, progress, stage, status="running", result_json=None):
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as conn:
            conn.execute("PRAGMA busy_timeout=15000")
            if result_json is not None:
                conn.execute(
                    "UPDATE scans SET progress=?, stage=?, status=?, result_json=? WHERE id=?",
                    (progress, stage, status, json.dumps(result_json), scan_id)
                )
            else:
                conn.execute(
                    "UPDATE scans SET progress=?, stage=?, status=? WHERE id=?",
                    (progress, stage, status, scan_id)
                )
            conn.commit()
    except Exception as e:
        # Antes era 'pass' silencioso: um lock aqui derrubava a barra sem deixar rastro.
        logger.warning(f"[SecOps] Falha ao atualizar progresso do scan {scan_id}: {e}")

def start_scan_thread(target, username, tts_enabled=False, session_id=None):
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

    with sqlite3.connect(DB_PATH, timeout=20) as conn:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute(
            "INSERT INTO scans (id, target, progress, stage, status, owner, session_id) VALUES (?, ?, 0, 'Inicializando...', 'running', ?, ?)",
            (scan_id, target, username, session_id)
        )
        conn.commit()

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
                        f"Você é um consultor de segurança ofensiva sênior (OSCP/Red Team) redigindo um "
                        f"relatório executivo formal para a diretoria e para a equipe técnica de um cliente "
                        f"corporativo. Idioma: Português do Brasil. Formato: Markdown, tom profissional, "
                        f"objetivo e sem sensacionalismo.\n\n"
                        f"ALVO AUDITADO: {target}\n"
                        f"CVEs DETECTADAS PELO SCANNER (use APENAS estas, não invente outras): {lista_cves}\n\n"
                        f"Estruture EXATAMENTE nesta ordem, usando cabeçalhos '##':\n"
                        f"## 1. Sumário Executivo\n"
                        f"- Classifique o risco geral do alvo como CRÍTICO, ALTO, MÉDIO ou BAIXO e justifique "
                        f"em 2-3 frases voltadas ao impacto de negócio (exposição, continuidade, dados).\n"
                        f"## 2. Superfície de Ataque\n"
                        f"- Liste em bullets cada porta/serviço/versão exposto e o que ele representa em risco.\n"
                        f"## 3. Análise de Vulnerabilidades\n"
                        f"- Para cada CVE relevante: cite o identificador em TEXTO PURO (ex.: CVE-2011-3192, "
                        f"sem criar hyperlink), a severidade/CVSS quando houver, uma descrição técnica curta, "
                        f"o vetor de exploração e o impacto potencial.\n"
                        f"## 4. Plano de Remediação Priorizado\n"
                        f"- Ações concretas em ordem de prioridade, com esforço estimado (Baixo/Médio/Alto) e "
                        f"prazo sugerido (Imediato / 30 dias / 90 dias).\n"
                        f"## 5. Observações e Limitações\n"
                        f"- Ressalvas do scan (ex.: detecção sem autenticação, possíveis falsos positivos).\n\n"
                        f"REGRAS: não use tabelas Markdown (use listas com marcadores e rótulos em **negrito**). "
                        f"Não invente CVEs fora da lista fornecida. Seja técnico e direto.\n\n"
                        f"DADOS BRUTOS DO NMAP (JSON): {json.dumps(scan_data)}"
                    )
                    ai_analysis = exec_chat_with_oracle(prompt, username)

                    # Bloco de referências construído pelo código: garante que as CVEs
                    # apareçam mesmo que a IA esqueça alguma. Os IDs em texto puro são
                    # transformados em links clicáveis no chat e no relatório (via render).
                    report_md = ai_analysis
                    if cves:
                        linhas = "\n".join(
                            f"- {cve}" + (f"  —  CVSS {score}" if score else "")
                            for cve, score in cves
                        )
                        report_md += f"\n\n## 6. Referências (CVEs Identificadas)\n{linhas}"

                    resultado_final = f"🛡️ **Nmap scan report para {target}:**\n\n{report_md}\n\n[BOTÃO_DOWNLOAD:{scan_id}]"
                    result_payload = {"scan_data": scan_data, "ai_analysis": report_md}
                    final_status = "finished"
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
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute(
                    "INSERT INTO chat_history (username, sender, content, is_html, timestamp, session_id) VALUES (?, 'bot', ?, 0, ?, ?)",
                    (username, resultado_final, now_str, session_id)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"[SecOps] Erro ao gravar relatório no chat: {e}")

        # 2) SÓ DEPOIS marca o scan como concluído (o que o remove de /api/active_scans).
        #    Assim, quando o frontend perceber o fim e recarregar o histórico,
        #    a mensagem JÁ estará garantidamente gravada. Isso elimina a corrida
        #    que fazia o relatório "sumir" e nunca aparecer no chat.
        update_scan_progress(scan_id, 100, "Concluído", final_status, result_payload)

    threading.Thread(target=run_scan, daemon=True).start()
    return scan_id
