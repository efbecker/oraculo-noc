import os
import re
import json
import sqlite3
import logging
import oci
from datetime import datetime
from config import OCI_ENDPOINT, OCI_COMPARTMENT_ID, DB_PATH
from app.services.secops import start_scan_thread
from app.services.report_jobs import start_report_job, FIRST_STAGE as REPORT_FIRST_STAGE
from app.services.zabbix import (
    consultar_status_host_zabbix,
    consultar_incidentes_zabbix,
    gerar_grafico_zabbix,
    gerar_relatorio_consumo_todos
)

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
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            c = conn.cursor()
            c.execute("SELECT ai_model FROM users WHERE username=?", (username,))
            row = c.fetchone()
            return row[0] if (row and row[0]) else "cohere.command-r-08-2024"
    except: return "cohere.command-r-08-2024"

def get_user_profile(username):
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            c = conn.cursor()
            c.execute("SELECT profile FROM users WHERE username=?", (username,))
            row = c.fetchone()
            return str(row[0]).upper().strip() if (row and row[0]) else "CLIENTE"
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
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            c = conn.cursor()
            c.execute("SELECT zabbix_url FROM users WHERE username=?", (username,))
            row = c.fetchone()
            return str(row[0]).strip() if (row and row[0]) else None
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
    model_id = get_user_model(username)
    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        client = oci.generative_ai_inference.GenerativeAiInferenceClient(
            config={}, signer=signer, service_endpoint=OCI_ENDPOINT, retry_strategy=oci.retry.NoneRetryStrategy()
        )
        
        preamble = f"""Você é o Oráculo, um especialista de NOC Sênior. 
        SEMPRE consulte o contexto abaixo para responder. Se o usuário perguntar por algo que está no contexto, use os dados exatos.
        CONTEXTO ATUAL: {context}
        Seja direto, técnico e use bullet points."""
        
        cohere_history = []
        if chat_history_rows:
            for row in chat_history_rows:
                if row['is_html'] == 1: continue
                role = "USER" if row['sender'] == 'user' else "CHATBOT"
                cohere_history.append(oci.generative_ai_inference.models.CohereChatBotMessage(role=role, message=row['content']))
                
        chat_request = oci.generative_ai_inference.models.CohereChatRequest(
            api_format="COHERE", message=prompt, chat_history=cohere_history, preamble_override=preamble, max_tokens=2048, temperature=0.3
        )
        details = oci.generative_ai_inference.models.ChatDetails(
            compartment_id=OCI_COMPARTMENT_ID, serving_mode=oci.generative_ai_inference.models.OnDemandServingMode(model_id=model_id), chat_request=chat_request
        )
        resp = client.chat(details)
        return resp.data.chat_response.text
    except Exception as e:
        logger.error(f"[IA Oracle] Erro para o usuário '{username}': {str(e)}")
        return f"⚠️ **Falha de IA (Oracle OCI):**\nO Zabbix enviou os dados, mas a IA recusou a conexão.\nErro: `{str(e)}`"

def process_oraculo_message(message, username, token_zabbix, grupo_cliente, tts_enabled=False, history_rows=None, zabbix_url=None):
    if not zabbix_url: zabbix_url = get_user_zabbix_url(username)
    
    msg_l = message.lower()

    if any(p in msg_l for p in ['grafico', 'gráfico', 'consumo', 'relatorio', 'relatório']):
        dias = 1.0
        dias_match = re.search(r'(\d+)\s*dias?', msg_l)
        if dias_match: dias = float(dias_match.group(1))
        
        target = "TODOS"
        target_match = re.search(r'(?:do|da|servidor|vm|host)\s+([a-zA-Z0-9_-]+)', msg_l)
        if target_match: target = target_match.group(1)

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
                return gerar_grafico_zabbix(target, metrica, dias, token_zabbix, zabbix_url)

        job_id = start_report_job(username, label, _gerar)
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
        
        scan_id = start_scan_thread(target, username, tts_enabled)
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
