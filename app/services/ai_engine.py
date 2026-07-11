import os
import re
import json
import logging
from datetime import datetime
from app import db
from app.services import ai_providers
from app.services.secops import start_scan_thread
from app.services.report_jobs import start_report_job, FIRST_STAGE as REPORT_FIRST_STAGE
from app.services.zabbix import (
    consultar_status_host_zabbix,
    consultar_incidentes_zabbix,
    gerar_grafico_zabbix,
    gerar_relatorio_consumo_todos
)
from app.services.anomalias import detectar_anomalias_zabbix
from app.services.capacidade import prever_capacidade_zabbix

# ==========================================
# CONFIGURAÇÃO DO SISTEMA DE LOGS / AUDITORIA
# ==========================================
LOG_DIR = '/opt/oraculo-noc/logs'
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, 'oraculo_secops.log'),
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%d/%m/%Y %H:%M:%S'
)
logger = logging.getLogger('Oraculo')

def get_user_model(username):
    try:
        row = db.query_one("SELECT ai_model FROM users WHERE username=?", (username,))
        return row['ai_model'] if (row and row['ai_model']) else "cohere.command-r-08-2024"
    except: return "cohere.command-r-08-2024"

def get_user_profile(username):
    try:
        row = db.query_one("SELECT profile FROM users WHERE username=?", (username,))
        return str(row['profile']).upper().strip() if (row and row['profile']) else "CLIENTE"
    except: return "CLIENTE"

# ==========================================================================
# AUTORIZAÇÃO DE FERRAMENTAS OFENSIVAS (Nmap / SecOps)
# --------------------------------------------------------------------------
# Apenas ADMINISTRADOR pode disparar o scan profundo. A comparação é EXATA
# (perfil normalizado), evitando falsos positivos de substring.
# Para também liberar o NOC, basta adicionar "NOC" ao conjunto abaixo.
# ==========================================================================
PERFIS_AUTORIZADOS_SCAN = {"ADMIN", "ADMINISTRADOR"}

def perfil_pode_escanear(perfil):
    return str(perfil).upper().strip() in PERFIS_AUTORIZADOS_SCAN

def get_user_zabbix_url(username):
    try:
        row = db.query_one("SELECT zabbix_url FROM users WHERE username=?", (username,))
        return str(row['zabbix_url']).strip() if (row and row['zabbix_url']) else None
    except: return None

def obter_contexto_zabbix_real_time(token, grupo, url=None):
    if not token or token.strip() == "":
        return "ERRO_ZABBIX: Token da API não configurado."
    try:
        status = consultar_status_host_zabbix("", token, grupo, url)
        if status and "Erro de comunicação" in status:
            return f"ERRO_ZABBIX: {status}"
            
        incidentes = consultar_incidentes_zabbix("", 1.0, token, grupo, "", url)
        if incidentes and "Erro na coleta" in incidentes:
            return f"ERRO_ZABBIX: {incidentes}"
            
        return f"\n--- ESTADO ATUAL DA INFRAESTRUTURA ---\n{status}\n{incidentes}"
    except Exception as e: 
        return f"ERRO_ZABBIX: {str(e)}"

def exec_chat_with_oracle(prompt, username="admin", context="", chat_history_rows=None):
    """Envia a pergunta para a IA configurada NO PAINEL (Admin > Inteligência
    Artificial) — pode ser OCI, OpenAI, Gemini, Anthropic ou qualquer endpoint
    compatível (Groq, DeepSeek, Ollama local...).

    MULTI-TENANT: se o usuário tiver um PERFIL de IA vinculado (Admin > Usuários
    > campo 'Perfil de IA'), usa a config desse perfil — permitindo, por exemplo,
    o cliente Pollux no Gemini, o ASV no GPT e o INLOG no Claude, cada um com a
    SUA chave. Sem perfil vinculado, usa a configuração global padrão."""
    cfg = ai_providers.config_for_user(username)

    preamble = f"""Você é o Oráculo, um especialista de NOC Sênior.
    SEMPRE consulte o contexto abaixo para responder. Se o usuário perguntar por algo que está no contexto, use os dados exatos.
    CONTEXTO ATUAL: {context}
    Seja direto, técnico e use bullet points."""

    # Histórico normalizado para o formato neutro (role, content). As respostas
    # HTML (relatórios/gráficos) são puladas: são enormes e não ajudam o modelo.
    history = []
    if chat_history_rows:
        for row in chat_history_rows:
            if row['is_html'] == 1:
                continue
            role = 'user' if row['sender'] == 'user' else 'assistant'
            history.append((role, row['content']))

    try:
        return ai_providers.chat(prompt, preamble, history,
                                 modelo_usuario=get_user_model(username), cfg=cfg)
    except Exception as e:
        provedor = ai_providers.PROVIDER_LABELS.get(cfg.get('provider', ''), cfg.get('provider', '?'))
        logger.error(f"[IA:{cfg.get('provider')}] Erro para o usuário '{username}': {e}")
        return (f"⚠️ **Falha de IA ({provedor}):**\nOs dados do Zabbix vieram, mas a IA recusou a conexão.\n"
                f"Erro: `{e}`\n\n*Verifique o provedor, a chave e o modelo em Admin → Inteligência Artificial.*")


def process_oraculo_message(message, username, token_zabbix, grupo_cliente, tts_enabled=False, history_rows=None, zabbix_url=None, session_id=None):
    if not zabbix_url: zabbix_url = get_user_zabbix_url(username)
    
    msg_l = message.lower()

    # ======================================================================
    # DETECÇÃO DE ANOMALIAS (z-score) — este bloco vem ANTES do bloco de
    # relatório de propósito: "relatório de anomalias" contém 'relatorio' e,
    # na ordem antiga, cairia no FinOps em vez de cair aqui.
    # ======================================================================
    if any(p in msg_l for p in ['anomal', 'anômal', 'fora do normal', 'comportamento estranho',
                                 'algo estranho', 'estranho no ambiente', 'desvio de comportamento']):
        dias = 7  # baseline padrão: 1 semana de trends horários
        dias_match = re.search(r'(\d+)\s*dias?', msg_l)
        if dias_match: dias = int(dias_match.group(1))
        dias = max(2, min(dias, 30))

        logger.info(f"[Anomalias] Usuário '{username}' solicitou detecção de anomalias (baseline {dias} dias).")
        label = f"Detecção de Anomalias (baseline {dias} dias)"

        def _gerar():
            return detectar_anomalias_zabbix(dias, token_zabbix, grupo_cliente, zabbix_url)

        job_id = start_report_job(username, label, _gerar, session_id=session_id)
        if not job_id:
            return {"reply": "⚠️ Não foi possível iniciar a análise. Tente novamente.", "audio_path": None, "is_html": 0}

        intro = ("🔍 **Detecção de Anomalias iniciada!**\nEstou comparando o comportamento da última hora "
                 f"de cada métrica contra o padrão dos últimos {dias} dias.\n\n"
                 "*Assim que terminar, o resultado aparecerá automaticamente aqui no chat.*")
        return {"reply": intro, "action": "scan", "target": label, "scan_id": job_id, "stage": REPORT_FIRST_STAGE, "is_html": 0}

    # ======================================================================
    # PREVISÃO DE CAPACIDADE (regressão linear de discos) — idem: antes do
    # bloco de relatório para "relatório de capacidade" cair aqui.
    # ======================================================================
    if any(p in msg_l for p in ['capacidade', 'vai encher', 'quando enche', 'esgot',
                                 'forecast', 'projeç', 'projec',
                                 'previsão de disco', 'previsao de disco',
                                 'tendência de disco', 'tendencia de disco']):
        dias = 14  # histórico padrão da regressão: 2 semanas
        dias_match = re.search(r'(\d+)\s*dias?', msg_l)
        if dias_match: dias = int(dias_match.group(1))
        dias = max(7, min(dias, 90))

        logger.info(f"[Capacidade] Usuário '{username}' solicitou previsão de capacidade ({dias} dias de histórico).")
        label = f"Previsão de Capacidade ({dias} dias de histórico)"

        def _gerar():
            return prever_capacidade_zabbix(dias, token_zabbix, grupo_cliente, zabbix_url)

        job_id = start_report_job(username, label, _gerar, session_id=session_id)
        if not job_id:
            return {"reply": "⚠️ Não foi possível iniciar a análise. Tente novamente.", "audio_path": None, "is_html": 0}

        intro = ("📈 **Previsão de Capacidade iniciada!**\nEstou projetando a tendência de crescimento "
                 f"de discos, CPU, memória e interfaces de rede com base nos últimos {dias} dias.\n\n"
                 "*Assim que terminar, o resultado aparecerá automaticamente aqui no chat.*")
        return {"reply": intro, "action": "scan", "target": label, "scan_id": job_id, "stage": REPORT_FIRST_STAGE, "is_html": 0}

    if any(p in msg_l for p in ['grafico', 'gráfico', 'consumo', 'relatorio', 'relatório']):
        dias = 1.0
        dias_match = re.search(r'(\d+)\s*dias?', msg_l)
        if dias_match: dias = float(dias_match.group(1))
        dias = max(dias, 1.0)  # prazo mínimo: nunca gera relatório com menos de 1 dia de histórico
        
        target = "TODOS"
        # Só assume um alvo específico com uma palavra-chave INEQUÍVOCA na frente
        # (servidor/vm/host/máquina/aplicação). "do"/"da" sozinhos são gatilhos
        # genéricos demais: em "relatório da minha infraestrutura", isso capturava
        # "minha" como se fosse hostname, e o relatório saía vazio (host inexistente).
        target_match = re.search(r'(?:servidor|vm|host|maquina|máquina|aplicacao|aplicação|app)\s+([a-zA-Z0-9_.-]+)', msg_l)
        if target_match:
            candidato = target_match.group(1).strip('.')
            # Segunda camada de proteção: nunca aceita palavras comuns do português
            # como se fossem nome de host, mesmo que um gatilho tenha "pegado" algo.
            palavras_bloqueadas = {
                "minha", "minhas", "meu", "meus", "nossa", "nossas", "nosso", "nossos",
                "infraestrutura", "infra", "rede", "ambiente", "empresa", "cliente",
                "conta", "todos", "todas", "geral", "completo", "atual", "aqui"
            }
            if candidato and candidato not in palavras_bloqueadas and len(candidato) > 2:
                target = candidato

        metrica = "CPU"
        if any(x in msg_l for x in ['memoria', 'memória']): metrica = "MEMORIA"
        elif 'disco' in msg_l: metrica = "DISCO"
        elif 'rede' in msg_l: metrica = "REDE"
        
        logger.info(f"[Relatório] Usuário '{username}' solicitou relatório FinOps/Consumo para alvo: {target} ({dias} dias).")

        # Fecha as credenciais numa função geradora; o relatório em si (formato
        # HTML) permanece EXATAMENTE o mesmo, apenas passa a rodar em background.
        dias_txt = int(dias) if float(dias).is_integer() else dias
        if target == "TODOS":
            label = f"Relatório FinOps ({dias_txt} dia(s))"
            def _gerar():
                return gerar_relatorio_consumo_todos(dias, token_zabbix, grupo_cliente, zabbix_url)
        else:
            label = f"Relatório {target} ({dias_txt} dia(s))"
            def _gerar():
                return gerar_grafico_zabbix(target, metrica, dias, token_zabbix, grupo_cliente, zabbix_url)

        job_id = start_report_job(username, label, _gerar, session_id=session_id)
        if not job_id:
            return {"reply": "⚠️ Não foi possível iniciar o relatório. Tente novamente.", "audio_path": None, "is_html": 0}

        intro = ("📊 **Relatório em processamento!**\nEstou coletando os dados do seu ambiente no Zabbix.\n\n"
                 "*Assim que terminar, o relatório aparecerá automaticamente aqui no chat.*")
        return {"reply": intro, "action": "scan", "target": label, "scan_id": job_id, "stage": REPORT_FIRST_STAGE, "is_html": 0}

    # SecOps - Nmap Scan (APENAS ADMINISTRADOR)
    if any(p in msg_l for p in ['scan', 'nmap', 'secops', 'vulnerabilidade']):
        perfil = get_user_profile(username)
        if not perfil_pode_escanear(perfil):
            logger.warning(f"[SecOps BLOCKED] Scan NEGADO. Usuário '{username}' (Perfil: {perfil}) tentou acionar ferramenta ofensiva.")
            return {
                "reply": "🛡️ **Acesso negado.**\nA varredura de segurança (Nmap) é uma ferramenta ofensiva restrita a **administradores**. Seu perfil não tem permissão para executá-la.",
                "audio_path": None,
                "is_html": 0
            }
            
        ip_match = re.search(r'\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}|\b(?:\d{1,3}\.){3}\d{1,3}\b', msg_l)
        target = ip_match.group(0) if ip_match else "127.0.0.1"
        
        # LOG CRÍTICO DE AUDITORIA
        logger.warning(f"[SecOps AUDIT] 🚨 Usuário '{username}' (Perfil: {perfil}) INICIOU scan profundo no alvo: {target}.")
        
        scan_id = start_scan_thread(target, username, tts_enabled, session_id=session_id)
        if not scan_id:
            return {
                "reply": "🛡️ **Acesso negado.**\nA varredura de segurança (Nmap) é restrita a **administradores**.",
                "audio_path": None,
                "is_html": 0
            }
        msg_reply = f"🛡️ **SecOps Ativado!**\nIniciando scan profundo no alvo: `{target}`.\n\n*O processo está rodando em background. Assim que o scan terminar, o relatório será gerado automaticamente aqui no chat.*"
        
        return {"reply": msg_reply, "action": "scan", "target": target, "scan_id": scan_id, "is_html": 0}

    # IA Oracle
    context = obter_contexto_zabbix_real_time(token_zabbix, grupo_cliente, zabbix_url)
    if "ERRO_ZABBIX" in context:
        logger.error(f"[Zabbix] Falha de comunicação para o usuário '{username}': {context}")
        return {
            "reply": f"🚨 **Não consegui conectar ao Zabbix!**\n\nMotivo:\n`{context}`", 
            "audio_path": None, 
            "is_html": 0
        }

    logger.info(f"[IA Oracle] Usuário '{username}' iniciou interação livre com o modelo.")
    reply = exec_chat_with_oracle(message, username=username, context=context, chat_history_rows=history_rows)
    return {"reply": reply, "audio_path": None}
