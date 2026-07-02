import os
import re
import ipaddress
import subprocess
import xml.etree.ElementTree as ET
import threading
import uuid
import tempfile
import json
import sqlite3
import asyncio
import edge_tts
from datetime import datetime
from config import DB_PATH, AUDIO_DIR

# ================= HELPER TTS (EDGE-TTS) =================
async def _generate_audio(text, output_path):
    VOICE = "pt-BR-FranciscaNeural" 
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(output_path)

def create_tts_audio(text, scan_id=None):
    clean_text = re.sub(r'<[^>]*>?', '', text)
    clean_text = re.sub(r'[*#_`]', '', clean_text)
    clean_text = re.sub(r'\[BOTÃO_DOWNLOAD:.*?\]', 'O botão para download do relatório executivo está disponível na tela.', clean_text)
    clean_text = re.sub(r'https?:\/\/[^\s]+', 'link', clean_text)
    
    filename = f"tts_{uuid.uuid4().hex}.mp3"
    if scan_id: filename = f"scan_{scan_id}.mp3"
    
    filepath = os.path.join(AUDIO_DIR, filename)
    asyncio.run(_generate_audio(clean_text, filepath))
    return filename

# ================= UTILS E NMAP =================
def validate_target(target):
    try: 
        ipaddress.ip_address(target)
        return True
    except ValueError: 
        return bool(re.match(r"^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$", target))

def parse_nmap_xml_file(file_path):
    try:
        tree = ET.parse(file_path)
        hosts = []
        for host in tree.getroot().findall("host"):
            addr = host.find("address")
            ip = addr.get("addr") if addr is not None else "Desconhecido"
            ports = []
            for port in host.findall(".//port"):
                state = port.find("state").get("state")
                if state != "open": continue
                port_id = port.get("portid")
                proto = port.get("protocol")
                service = port.find("service")
                svc_name = service.get("name") if service is not None else "unknown"
                version = service.get("version") if service is not None else ""
                vulns = [{"id": s.get("id", ""), "output": s.get("output", "")} for s in port.findall("script")]
                ports.append({"id": f"{port_id}/{proto}", "service": svc_name, "version": version, "vulns": vulns})
            hosts.append({"ip": ip, "ports": ports})
        return hosts
    except Exception: return []

def update_scan_in_db(scan_id, status, progress, stage, result_json=None):
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            if result_json:
                conn.execute("UPDATE scans SET status=?, progress=?, stage=?, result_json=? WHERE id=?", 
                             (status, progress, stage, json.dumps(result_json), scan_id))
            else:
                conn.execute("UPDATE scans SET status=?, progress=?, stage=? WHERE id=?", 
                             (status, progress, stage, scan_id))
            conn.commit()
    except Exception:
        pass

def run_nmap_worker(scan_id, target, username, enable_tts=False):
    # Importação local para evitar ciclo de imports
    from app.services.ai_engine import analyze_secops_with_oracle

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as temp_xml: 
        xml_path = temp_xml.name
        
    command = ["nmap", "-Pn"] if os.geteuid() != 0 else ["nmap", "-Pn"]
    if os.geteuid() == 0: command += ["-sS", "-sU", "--top-ports", "100", "-O"]
    else: command += ["-sT"]
    command += ["-A", "--script", "vuln,smb-os-discovery,http-title", "-T4", "-v", "--stats-every", "1s", "-oX", xml_path, target]
    
    last_progress, last_stage = -1, ""
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None: break
            if line:
                line = line.strip()
                stage = None
                if "Ping Scan" in line: stage = "Conectividade"
                elif "Syn" in line or "Connect" in line: stage = "Varredura TCP"
                elif "Service scan" in line: stage = "Análise de Serviços"
                elif "NSE" in line: stage = "Checagem de Vulnerabilidades"
                
                progress = None
                match = re.search(r"About\s+(\d+(\.\d+)?)%", line)
                if match: progress = float(match.group(1))
                
                if (stage and stage != last_stage) or (progress and progress != last_progress):
                    update_scan_in_db(scan_id, 'running', progress or last_progress, stage or last_stage)
                    if stage: last_stage = stage
                    if progress: last_progress = progress

        if process.wait() == 0:
            update_scan_in_db(scan_id, "running", 95, "Gerando relatório com IA OCI...")
            scan_data = parse_nmap_xml_file(xml_path)
            if scan_data:
                ai_analysis = analyze_secops_with_oracle(scan_data, target)
                update_scan_in_db(scan_id, "finished", 100, "Concluído", {"scan_data": scan_data, "ai_analysis": ai_analysis})
                
                audio_filename = create_tts_audio(ai_analysis, scan_id) if enable_tts else None
                
                with sqlite3.connect(DB_PATH, timeout=15) as conn:
                    conn.execute("INSERT INTO chat_messages (username, sender, content, is_html, audio_path) VALUES (?, ?, ?, 0, ?)", 
                                 (username, 'ai', f"{ai_analysis}\n\n[BOTÃO_DOWNLOAD:{scan_id}]", audio_filename))
                    conn.commit()
            else:
                msg_erro = "Nenhum Host Ativo Detectado ou firewall bloqueando severamente."
                update_scan_in_db(scan_id, "error", 100, msg_erro)
                audio_filename = create_tts_audio(f"Atenção. Falha no rastreio. {msg_erro}") if enable_tts else None
                with sqlite3.connect(DB_PATH, timeout=15) as conn:
                    conn.execute("INSERT INTO chat_messages (username, sender, content, is_html, audio_path) VALUES (?, ?, ?, 0, ?)", 
                                 (username, 'ai', f"⚠️ **Falha no scan:** {msg_erro}", audio_filename))
                    conn.commit()
        else:
            msg_erro = "Erro interno na execução do Nmap."
            update_scan_in_db(scan_id, "error", 100, msg_erro)
            audio_filename = create_tts_audio(f"Atenção. Falha interna do Nmap. {msg_erro}") if enable_tts else None
            with sqlite3.connect(DB_PATH, timeout=15) as conn:
                conn.execute("INSERT INTO chat_messages (username, sender, content, is_html, audio_path) VALUES (?, ?, ?, 0, ?)", 
                             (username, 'ai', f"⚠️ **Falha no scan:** {msg_erro}", audio_filename))
                conn.commit()
    except Exception as e:
        update_scan_in_db(scan_id, "error", 100, str(e))
        audio_filename = create_tts_audio("Atenção. Ocorreu um erro no servidor durante a execução do comando Nmap.") if enable_tts else None
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.execute("INSERT INTO chat_messages (username, sender, content, is_html, audio_path) VALUES (?, ?, ?, 0, ?)", 
                         (username, 'ai', f"⚠️ **Falha no scan:** {str(e)}", audio_filename))
            conn.commit()
    finally:
        if os.path.exists(xml_path): os.remove(xml_path)

def start_scan_thread(target, username, tts_enabled):
    scan_id = str(uuid.uuid4())
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        conn.execute("INSERT INTO scans (id, timestamp, target, mode, status, progress, stage, owner) VALUES (?, ?, ?, ?, 'running', 0, 'Iniciando engine...', ?)", 
                     (scan_id, datetime.now().strftime("%d/%m/%Y %H:%M"), target, 'deep', username))
        conn.execute("INSERT INTO chat_messages (username, sender, content, is_html) VALUES (?, ?, ?, 0)", 
                     (username, 'ai', f"Iniciando rastreio profundo em **{target}**. Acompanhe o progresso do radar:"))
        conn.commit()
    
    threading.Thread(target=run_nmap_worker, args=(scan_id, target, username, tts_enabled), daemon=True).start()
    return scan_id
