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
        grupo_cliente=session.get('grupo_cliente'), cache_buster=str(__import__('time').time())
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

@bp.route('/api/active_scans', methods=['GET'])
@login_required
def get_active_scans():
    try:
        import sqlite3
        from config import DB_PATH
        username = session.get('username')
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            # Isolamento multi-tenant: cada usuário só enxerga os SEUS scans em execução.
            rows = conn.execute(
                "SELECT id, target, progress, stage FROM scans WHERE status='running' AND owner=?",
                (username,)
            ).fetchall()
            return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify([])

@bp.route('/api/download/<scan_id>', methods=['GET'])
@login_required
def download_report(scan_id):
    try:
        import sqlite3
        from config import DB_PATH
        import json
        from flask import Response
        import re
        from datetime import datetime
        
        username = session.get('username')
        profile = str(session.get('profile') or '').upper().strip()
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            row = conn.execute("SELECT target, result_json, owner FROM scans WHERE id = ?", (scan_id,)).fetchone()
            if not row or not row[1]: return "Relatório não disponível.", 404

            # Só o dono do scan (ou um administrador) pode baixar o relatório.
            owner = row[2]
            if owner != username and profile not in ('ADMIN', 'ADMINISTRADOR'):
                return "Acesso negado.", 403

            target = row[0]
            ai_text = json.loads(row[1]).get("ai_analysis", "")
            
            # Transforma cada CVE-xxxx-xxxx em link clicável para o NVD (antes das demais conversões).
            ai_text = re.sub(
                r'\bCVE-\d{4}-\d{4,7}\b',
                lambda m: f'<a href="https://nvd.nist.gov/vuln/detail/{m.group(0)}" target="_blank" style="color:#c0392b; font-weight:bold">{m.group(0)}</a>',
                ai_text
            )
            html_content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', ai_text)
            html_content = re.sub(r'### (.*?)\n', r'<h3>\1</h3>\n', html_content)
            html_content = re.sub(r'## (.*?)\n', r'<h2>\1</h2>\n', html_content)
            html_content = re.sub(r'# (.*?)\n', r'<h1>\1</h1>\n', html_content)
            html_content = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2" target="_blank">\1</a>', html_content)
            html_content = html_content.replace('\n', '<br>')
            
            html = f"""
            <html><head><meta charset="UTF-8"><title>Relatório - {target}</title>
            <style>body{{font-family:sans-serif; padding:40px; line-height:1.6; color:#333; max-width:900px; margin:auto}} h1, h2, h3{{color:#0077ff}} .code{{background:#f4f4f4; padding:20px; border-radius:8px}} a{{color:#2ea043; font-weight:bold}}</style>
            </head><body>
            <h1>Oráculo SecOps - Relatório Executivo</h1>
            <hr><p><b>Alvo Auditado:</b> {target}<br><b>Data/Hora:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
            <div class='code'>{html_content}</div>
            <p style='text-align:center; font-size:0.8em; margin-top:50px'>Gerado por Oráculo Armazem Cloud</p>
            </body></html>
            """
            
            return Response(
                html,
                mimetype="text/html",
                headers={"Content-disposition": f"attachment; filename=Relatorio_SecOps_{target}.html"}
            )
    except Exception as e:
        return f"Erro ao gerar relatório: {str(e)}", 500
