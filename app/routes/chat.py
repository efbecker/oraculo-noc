import os
import re
import json
import sqlite3
from datetime import datetime
from flask import Blueprint, request, jsonify, session, render_template, send_file, Response
from config import DB_PATH, AUDIO_DIR
from app.routes.auth import login_required
from app.services.ai_engine import process_oraculo_message
from app.services.secops import create_tts_audio

bp = Blueprint('chat', __name__)

@bp.route("/", methods=["GET"])
@login_required
def index():
    return render_template('chat.html', 
                           username=session.get('username'), 
                           role=session.get('role'),
                           client_group=session.get('client_group'))

@bp.route("/api/chat", methods=["POST"])
@login_required
def chat_endpoint():
    data = request.json
    message = data.get("message", "")
    tts_enabled = data.get("tts_enabled", False)
    username = session.get('username')
    token_zabbix = session.get('zabbix_token')
    grupo_cliente = session.get('client_group')
    
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        history_rows = [dict(r) for r in conn.execute("SELECT sender, content, is_html FROM chat_messages WHERE username=? AND date(timestamp)=date('now') ORDER BY id ASC LIMIT 20", (username,)).fetchall()]
        
        # Ignora mensagens marcadas como silent (não salva do banco)
        if not data.get("silent"):
            conn.execute("INSERT INTO chat_messages (username, sender, content, is_html) VALUES (?, ?, ?, 0)", (username, 'user', message))
            conn.commit()
    
    # Chama o motor central de Inteligência Artificial
    resultado = process_oraculo_message(message, username, token_zabbix, grupo_cliente, tts_enabled, history_rows)
    
    # Salva a resposta da IA no banco, caso não seja um início de Scan (que é salvo na Thread do SecOps)
    if resultado.get("action") != "scan":
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.execute("INSERT INTO chat_messages (username, sender, content, is_html, audio_path) VALUES (?, ?, ?, ?, ?)", 
                         (username, 'ai', resultado.get("reply", ""), resultado.get("is_html", 0), resultado.get("audio_path")))
            conn.commit()
            
    return jsonify(resultado)

@bp.route("/api/chat_history", methods=["GET"])
@login_required
def get_chat_history():
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        return jsonify([dict(r) for r in conn.execute("SELECT sender, content, is_html, date(timestamp) as chat_date FROM chat_messages WHERE username=? ORDER BY id ASC", (session.get('username'),)).fetchall()])

@bp.route("/api/chat_history/<date>", methods=["DELETE"])
@login_required
def delete_chat_history(date):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date): return jsonify({"error": "Data inválida"}), 400
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        conn.execute("DELETE FROM chat_messages WHERE username=? AND date(timestamp)=?", (session.get('username'), date))
        conn.commit()
    return jsonify({"status": "success"})

@bp.route("/api/tts", methods=["POST"])
@login_required
def generate_dynamic_tts():
    text = request.json.get("text", "")
    if not text: return jsonify({"error": "Sem texto"}), 400
    audio_filename = create_tts_audio(text)
    return jsonify({"audio_path": audio_filename})

@bp.route("/api/audio/<filename>")
@login_required
def get_audio(filename):
    filepath = os.path.join(AUDIO_DIR, filename)
    if os.path.exists(filepath): return send_file(filepath, mimetype="audio/mpeg")
    return "Arquivo não encontrado", 404

# --- ENDPOINTS DO VULN::HUNTER (SECOPS) ---
@bp.route("/api/active_scans", methods=["GET"])
@login_required
def get_active_scans():
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            return jsonify([dict(r) for r in conn.execute("SELECT id, target, progress, stage FROM scans WHERE status='running' AND owner=?", (session.get('username'),)).fetchall()])
    except: return jsonify([])

@bp.route("/api/history/<scan_id>", methods=["GET"])
@login_required
def get_history_detail(scan_id):
    tts_enabled = request.args.get('tts_enabled', 'false').lower() == 'true'
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        row = conn.execute("SELECT result_json, stage FROM scans WHERE id = ?", (scan_id,)).fetchone()
        
        if row and row[0]:
            data = json.loads(row[0])
            if tts_enabled and 'ai_analysis' in data and 'audio_path' not in data:
                 audio_path = create_tts_audio(data['ai_analysis'], scan_id)
                 data['audio_path'] = audio_path
            return jsonify(data)
        elif row:
            err_msg = f"O Nmap reportou falha: {row[1]}"
            audio_path = create_tts_audio(err_msg) if tts_enabled else None
            return jsonify({"error": err_msg, "audio_path": audio_path})
            
        return jsonify({"error": "Scan não encontrado"}), 404

@bp.route("/api/download/<scan_id>", methods=["GET"])
@login_required
def download_report(scan_id):
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        row = conn.execute("SELECT target, result_json FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if not row or not row[1]: return "Relatório não disponível.", 404
        
        target = row[0]
        ai_text = json.loads(row[1]).get("ai_analysis", "")
        
        # Formata o Markdown levemente para o HTML do relatório
        html_content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', ai_text)
        html_content = re.sub(r'### (.*?)\n', r'<h3>\1</h3>\n', html_content)
        html_content = re.sub(r'## (.*?)\n', r'<h2>\1</h2>\n', html_content)
        html_content = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2" target="_blank">\1</a>', html_content)
        html_content = html_content.replace('\n', '<br>')
        
        html = f"""
        <html><head><meta charset="UTF-8"><title>Relatório de Auditoria - {target}</title>
        <style>body{{font-family:sans-serif; padding:40px; line-height:1.6; color:#333; max-width:900px; margin:auto}} h1, h2, h3{{color:#1f6feb}} .code{{background:#f4f4f4; padding:20px; border-radius:8px}} a{{color:#2ea043; font-weight:bold}}</style>
        </head><body>
        <h1>Armazém Cloud - Relatório de Auditoria de SecOps</h1>
        <hr><p><b>Alvo Auditado:</b> {target}<br><b>Data/Hora:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
        <div class='code'>{html_content}</div>
        <p style='text-align:center; font-size:0.8em; margin-top:50px'>Gerado pelo Oráculo AI Engine</p>
        </body></html>
        """
        return Response(html, mimetype="text/html", headers={"Content-disposition": f"attachment; filename=Auditoria_SecOps_{target}.html"})
