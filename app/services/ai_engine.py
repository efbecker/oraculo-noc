import os
import re
import json
import sqlite3
import oci
from datetime import datetime
from config import OCI_ENDPOINT, OCI_COMPARTMENT_ID, DB_PATH
from app.services.secops import start_scan_thread, create_tts_audio
from app.services.zabbix import (
    consultar_status_host_zabbix,
    consultar_incidentes_zabbix,
    gerar_grafico_zabbix,
    gerar_relatorio_consumo_todos
)

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

def obter_contexto_zabbix_real_time(token, grupo):
    """O Radar da IA: Lê o estado atual do ambiente antes de responder qualquer coisa."""
    try:
        status = consultar_status_host_zabbix("", token, grupo)
        incidentes = consultar_incidentes_zabbix("", 1.0, token, grupo)
        return f"\n--- ESTADO ATUAL DA INFRAESTRUTURA ---\n{status}\n{incidentes}"
    except: return ""

def exec_chat_with_oracle(prompt, username="admin", context="", chat_history_rows=None):
    model_id = get_user_model(username)
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    client = oci.generative_ai_inference.GenerativeAiInferenceClient(
        config={}, signer=signer, service_endpoint=OCI_ENDPOINT, retry_strategy=oci.retry.NoneRetryStrategy()
    )
    
    # Injetamos o contexto de tempo real diretamente no preâmbulo da IA
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

def process_oraculo_message(message, username, token_zabbix, grupo_cliente, tts_enabled=False, history_rows=None):
    # 1. Pega o contexto real antes de decidir o que fazer
    context = obter_contexto_zabbix_real_time(token_zabbix, grupo_cliente)
    msg_l = message.lower()

    # 2. Rotinas automáticas de Relatórios
    if any(p in msg_l for p in ['grafico', 'gráfico', 'consumo', 'relatorio', 'relatório']):
        dias = 1.0
        dias_match = re.search(r'(\d+)\s*dias?', msg_l)
        if dias_match: dias = float(dias_match.group(1))
        
        target = "TODOS"
        target_match = re.search(r'(?:do|da|servidor|vm|host)\s+([a-zA-Z0-9_-]+)', msg_l)
        if target_match: target = target_match.group(1)
        
        if target == "TODOS":
            reply = gerar_relatorio_consumo_todos(dias, token_zabbix, grupo_cliente)
        else:
            metrica = "CPU"
            if any(x in msg_l for x in ['memoria', 'memória']): metrica = "MEMORIA"
            elif 'disco' in msg_l: metrica = "DISCO"
            elif 'rede' in msg_l: metrica = "REDE"
            reply = gerar_grafico_zabbix(target, metrica, dias, token_zabbix)
        
        return {"reply": reply, "audio_path": create_tts_audio("Relatório concluído, efetue o download.") if tts_enabled else None, "is_html": 1}

    # 3. Rotinas de Scan (com segurança)
    if any(p in msg_l for p in ['scan', 'nmap', 'secops', 'vulnerabilidade']):
        perfil = get_user_profile(username)
        if "ADMIN" not in perfil and "NOC" not in perfil and grupo_cliente != "GLOBAL":
            return {"reply": "🛡️ Acesso negado a ferramentas de segurança.", "audio_path": None}
        ip_match = re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', msg_l)
        target = ip_match.group(0) if ip_match else "127.0.0.1"
        scan_id = start_scan_thread(target, username, tts_enabled)
        return {"action": "scan", "target": target, "scan_id": scan_id}

    # 4. Chat Inteligente com Contexto Zabbix
    reply = exec_chat_with_oracle(message, username=username, context=context, chat_history_rows=history_rows)
    return {"reply": reply, "audio_path": create_tts_audio(reply) if tts_enabled else None}
