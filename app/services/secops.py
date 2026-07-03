import threading
import subprocess
import sqlite3
import logging
import uuid
import tempfile
import os
import re
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from config import DB_PATH

logger = logging.getLogger('Oraculo')

def init_scan_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                target TEXT,
                progress REAL DEFAULT 0,
                stage TEXT,
                status TEXT,
                result_json TEXT
            )
        ''')
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
    except Exception: return []

def update_scan_progress(scan_id, progress, stage, status="running", result_json=None):
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            if result_json:
                conn.execute("UPDATE scans SET progress=?, stage=?, status=?, result_json=? WHERE id=?", (progress, stage, status, json.dumps(result_json), scan_id))
            else:
                conn.execute("UPDATE scans SET progress=?, stage=?, status=? WHERE id=?", (progress, stage, status, scan_id))
            conn.commit()
    except Exception as e:
        pass

def start_scan_thread(target, username, tts_enabled=False):
    scan_id = str(uuid.uuid4())
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO scans (id, target, progress, stage, status) VALUES (?, ?, 0, 'Inicializando...', 'running')", (scan_id, target))
    
    def run_scan():
        from app.services.ai_engine import exec_chat_with_oracle
        
        logger.info(f"[SecOps] Varredura Progressiva em {target}")
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as temp_xml: 
            xml_path = temp_xml.name
            
        # NMAP COMANDO FORÇADO: A flag --packet-trace e -v forçam o output contínuo a evitar que o buffer empanque!
        comando = ["nmap", "-Pn", "-A", "--script", "vuln,smb-os-discovery,http-title", "-T4", "--stats-every", "2s", "-v", "-oX", xml_path, target]
        
        resultado_final = ""
        last_progress, last_stage = 0, "Inicializando..."
        
        try:
            # bufsize=1 e universal_newlines=True obrigam o subprocess a não guardar os dados
            process = subprocess.Popen(comando, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
            
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None: break
                if line:
                    line = line.strip(); stage = None
                    if "Ping Scan" in line: stage = "Conectividade"
                    elif "Syn" in line or "Connect" in line or "Initiating SYN" in line: stage = "Varredura TCP"
                    elif "Service scan" in line or "Initiating Service" in line: stage = "Análise de Serviços"
                    elif "NSE" in line or "NSE:" in line: stage = "Vulnerabilidades"
                    elif "OS detection" in line: stage = "Detecção de SO"
                    
                    progress = None
                    # Regex mais robusta para apanhar "About 35.5% done" ou "About 35%" 
                    match = re.search(r"About\s+(\d+(?:\.\d+)?)%", line)
                    if match: progress = float(match.group(1))
                    
                    if (stage and stage != last_stage) or (progress and progress != last_progress):
                        last_stage = stage if stage else last_stage
                        last_progress = progress if progress else last_progress
                        update_scan_progress(scan_id, last_progress, last_stage)

            if process.wait() == 0 or os.path.exists(xml_path):
                update_scan_progress(scan_id, 95, "Gerando relatório IA...")
                scan_data = parse_nmap_xml_file(xml_path)
                
                if scan_data:
                    prompt = f"Atue como Red Team Sênior. Analise este JSON do Nmap do alvo {target} e monte um relatório gerencial em Markdown (PT-BR) contendo: 1. Resumo Executivo (Risco). 2. Portas e Serviços mapeados. 3. Vulnerabilidades Críticas. 4. Mitigações recomendadas. JSON: {json.dumps(scan_data)}"
                    ai_analysis = exec_chat_with_oracle(prompt, username)
                    
                    resultado_final = f"🛡️ **Nmap scan report para {target}:**\n\n{ai_analysis}\n\n[BOTÃO_DOWNLOAD:{scan_id}]"
                    update_scan_progress(scan_id, 100, "Concluído", "finished", {"scan_data": scan_data, "ai_analysis": ai_analysis})
                else:
                    resultado_final = f"🛡️ **Nmap scan report:**\nNenhum Host Ativo Detectado."
                    update_scan_progress(scan_id, 100, "Concluído", "error")
            else:
                 resultado_final = f"🛡️ **Nmap scan report:**\nFalha ao executar o comando."
                 update_scan_progress(scan_id, 100, "Concluído", "error")
                 
        except Exception as e:
            resultado_final = f"🛡️ **Nmap scan report:**\nErro crítico: {str(e)}"
            update_scan_progress(scan_id, 100, "Concluído", "error")
        
        finally:
            if os.path.exists(xml_path): os.remove(xml_path)

        now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        try:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                conn.execute("INSERT INTO chat_history (username, sender, content, is_html, timestamp) VALUES (?, 'bot', ?, 0, ?)", 
                             (username, resultado_final, now_str))
        except Exception as e:
            logger.error(f"[SecOps] Erro DB: {e}")

    threading.Thread(target=run_scan).start()
    return scan_id
