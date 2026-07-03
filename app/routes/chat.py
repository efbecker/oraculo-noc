import sqlite3
from datetime import datetime
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from app.routes.auth import login_required
from app.services.ai_engine import process_oraculo_message
from config import DB_PATH

bp = Blueprint('chat', __name__)

@bp.route("/")
@bp.route("/chat")
@login_required
def index():
    return render_template(
        'chat.html', 
        username=session.get('username'), 
        profile=session.get('profile'),
        grupo_cliente=session.get('grupo_cliente')
    )

@bp.route("/api/chat/message", methods=["POST"])
@login_required
def send_message():
    data = request.json
    message = data.get("message", "").strip()
    tts_enabled = data.get("tts_enabled", False)
    
    if not message: return jsonify({"error": "Mensagem vazia"}), 400

    username = session.get('username')
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    
    token_zabbix = ""
    grupo_cliente = ""
    zabbix_url = ""
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT token_zabbix, grupo_cliente, zabbix_url FROM users WHERE username=?", (username,))
            row = c.fetchone()
            if row:
                token_zabbix = row['token_zabbix']
                grupo_cliente = row['grupo_cliente']
                zabbix_url = row['zabbix_url']
    except Exception as e: 
        return jsonify({"error": f"Erro credenciais: {str(e)}"}), 500

    chat_history_rows = []
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            # Salva a mensagem do usuário com a DATA/HORA
            conn.execute("INSERT INTO chat_history (username, sender, content, is_html, timestamp) VALUES (?, 'user', ?, 0, ?)", (username, message, now_str))
            
            c = conn.cursor()
            c.execute("SELECT sender, content, is_html FROM chat_history WHERE username=? ORDER BY id DESC LIMIT 10", (username,))
            chat_history_rows = list(c.fetchall())
            chat_history_rows.reverse()
            conn.commit()
    except Exception as e: 
        return jsonify({"error": f"Erro histórico: {str(e)}"}), 500

    res = process_oraculo_message(
        message, username, token_zabbix, grupo_cliente, tts_enabled=tts_enabled, 
        history_rows=chat_history_rows, zabbix_url=zabbix_url
    )

    reply = res.get("reply", "")
    is_html = res.get("is_html", 0)
    bot_time = datetime.now().strftime("%d/%m/%Y %H:%M")

    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            # Salva a resposta da IA com a DATA/HORA
            conn.execute("INSERT INTO chat_history (username, sender, content, is_html, timestamp) VALUES (?, 'bot', ?, ?, ?)", (username, reply, is_html, bot_time))
            conn.commit()
    except: pass

    res["timestamp"] = bot_time
    return jsonify(res)

@bp.route("/api/chat/history", methods=["GET"])
@login_required
def get_history():
    username = session.get('username')
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT sender, content, is_html, timestamp FROM chat_history WHERE username=? ORDER BY id ASC LIMIT 50", (username,))
            rows = c.fetchall()
            # Puxa o histórico enviando a data (se existir)
            return jsonify([{"sender": r['sender'], "content": r['content'], "is_html": r['is_html'], "timestamp": r['timestamp'] if r['timestamp'] else ""} for r in rows])
    except Exception as e: return jsonify({"error": str(e)}), 500

@bp.route("/api/chat/clear", methods=["POST"])
@login_required
def clear_history():
    username = session.get('username')
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.execute("DELETE FROM chat_history WHERE username=?", (username,))
            conn.commit()
        return jsonify({"status": "cleared"})
    except Exception as e: return jsonify({"error": str(e)}), 500
