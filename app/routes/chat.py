import sqlite3
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from app.routes.auth import login_required
from app.services.ai_engine import process_oraculo_message
from config import DB_PATH

bp = Blueprint('chat', __name__)

# BLINDAGEM CONTRA 404: Aceita tanto a raiz quanto /chat
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
    if not message: return jsonify({"error": "Mensagem vazia"}), 400

    username = session.get('username')
    
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
    except Exception as e: return jsonify({"error": f"Erro ao carregar credenciais: {str(e)}"}), 500

    chat_history_rows = []
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("INSERT INTO chat_history (username, sender, content, is_html) VALUES (?, 'user', ?, 0)", (username, message))
            c = conn.cursor()
            c.execute("SELECT sender, content, is_html FROM chat_history WHERE username=? ORDER BY id DESC LIMIT 10", (username,))
            chat_history_rows = list(c.fetchall())
            chat_history_rows.reverse()
            conn.commit()
    except Exception as e: return jsonify({"error": f"Erro no histórico: {str(e)}"}), 500

    res = process_oraculo_message(
        message, username, token_zabbix, grupo_cliente, tts_enabled=False, 
        history_rows=chat_history_rows, zabbix_url=zabbix_url
    )

    reply = res.get("reply", "")
    is_html = res.get("is_html", 0)

    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.execute("INSERT INTO chat_history (username, sender, content, is_html) VALUES (?, 'bot', ?, ?)", (username, reply, is_html))
            conn.commit()
    except: pass

    return jsonify(res)

@bp.route("/api/chat/history", methods=["GET"])
@login_required
def get_history():
    username = session.get('username')
    try:
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT sender, content, is_html FROM chat_history WHERE username=? ORDER BY id ASC LIMIT 50", (username,))
            rows = c.fetchall()
            return jsonify([{"sender": r['sender'], "content": r['content'], "is_html": r['is_html']} for r in rows])
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
