import threading
import subprocess
import sqlite3
import logging
from datetime import datetime
from config import DB_PATH

logger = logging.getLogger('Oraculo')

def start_scan_thread(target, username, tts_enabled=False):
    def run_scan():
        logger.info(f"[SecOps] Iniciando varredura otimizada em {target} para o user {username}")
        
        # Comando Otimizado: Scan profundo, mas com limite rígido de 4 minutos e apenas 1 retentativa
        comando = f"nmap -sV -sC -T4 --max-retries 1 --host-timeout 4m {target}"
        
        try:
            # Executa o Nmap no sistema operacional e captura o texto
            process = subprocess.Popen(comando, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = process.communicate()
            
            # Se o Nmap não devolver nada (bloqueado por WAF/Firewall)
            if not stdout or stdout.strip() == "":
                resultado = f"🛡️ **Nmap scan report:**\nO scan no alvo `{target}` estourou o tempo ou foi bloqueado. (Firewall agressivo ou host offline)."
            else:
                # Se o Nmap devolver dados, formata com a TAG exata que o Radar espera
                resultado = f"🛡️ **Nmap scan report para {target}:**\n<pre>{stdout}</pre>"
                
        except Exception as e:
            # Se o próprio Linux der erro
            resultado = f"🛡️ **Nmap scan report:**\nFalha crítica na execução do motor: {str(e)}"

        # ---------------------------------------------------------
        # INJEÇÃO OBRIGATÓRIA NA BASE DE DADOS PARA PARAR A BARRA
        # ---------------------------------------------------------
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        try:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                conn.execute("INSERT INTO chat_history (username, sender, content, is_html, timestamp) VALUES (?, 'bot', ?, 1, ?)", 
                             (username, resultado, now_str))
                conn.commit()
            logger.info(f"[SecOps] Relatório gerado e injetado com sucesso para {username}.")
        except Exception as e:
            logger.error(f"[SecOps] Falha fatal ao gravar na base de dados: {e}")

    # Inicia a Thread independente
    thread = threading.Thread(target=run_scan)
    thread.start()
    return "scan_started"
