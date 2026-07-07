import uuid
import json
import re
from datetime import datetime
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify, Response
from app.routes.auth import login_required
from app.services.ai_engine import process_oraculo_message
from app import db
from app.services import branding

bp = Blueprint('chat', __name__)


def _ensure_session(username, session_id):
    """Devolve um session_id válido do usuário. Se vier vazio/inválido, cria um novo."""
    try:
        if session_id:
            row = db.query_one(
                "SELECT id FROM chat_sessions WHERE id=? AND username=?", (session_id, username)
            )
            if row:
                return session_id
        sid = str(uuid.uuid4())
        now = datetime.now()
        db.execute(
            "INSERT INTO chat_sessions (id, username, created_at, created_ts) VALUES (?, ?, ?, ?)",
            (sid, username, now.strftime("%d/%m/%Y %H:%M"), now.timestamp())
        )
        return sid
    except Exception:
        return session_id or str(uuid.uuid4())


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
    session_id = _ensure_session(username, data.get("session_id"))
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    
    token_zabbix = ""
    grupo_cliente = ""
    zabbix_url = ""
    try:
        row = db.query_one("SELECT token_zabbix, grupo_cliente, zabbix_url FROM users WHERE username=?", (username,))
        if row:
            token_zabbix = row['token_zabbix']
            grupo_cliente = row['grupo_cliente']
            zabbix_url = row['zabbix_url']
    except Exception as e: 
        return jsonify({"error": f"Erro credenciais: {str(e)}"}), 500

    chat_history_rows = []
    try:
        # Salva a mensagem do usuário com a DATA/HORA
        db.execute(
            "INSERT INTO chat_history (username, sender, content, is_html, timestamp, session_id) VALUES (?, 'user', ?, 0, ?, ?)",
            (username, message, now_str, session_id)
        )
        rows = db.query_all(
            "SELECT sender, content, is_html FROM chat_history WHERE username=? AND session_id=? ORDER BY id DESC LIMIT 10",
            (username, session_id)
        )
        chat_history_rows = list(rows)
        chat_history_rows.reverse()
    except Exception as e: 
        return jsonify({"error": f"Erro histórico: {str(e)}"}), 500

    res = process_oraculo_message(
        message, username, token_zabbix, grupo_cliente, tts_enabled=tts_enabled, 
        history_rows=chat_history_rows, zabbix_url=zabbix_url, session_id=session_id
    )

    reply = res.get("reply", "")
    is_html = res.get("is_html", 0)
    bot_time = datetime.now().strftime("%d/%m/%Y %H:%M")

    try:
        # Salva a resposta da IA na MESMA sessão
        db.execute(
            "INSERT INTO chat_history (username, sender, content, is_html, timestamp, session_id) VALUES (?, 'bot', ?, ?, ?, ?)",
            (username, reply, is_html, bot_time, session_id)
        )
    except Exception:
        pass

    res["timestamp"] = bot_time
    res["session_id"] = session_id
    return jsonify(res)

@bp.route("/api/chat/history", methods=["GET"])
@login_required
def get_history():
    username = session.get('username')
    session_id = request.args.get("session_id")
    try:
        if session_id:
            rows = db.query_all(
                "SELECT sender, content, is_html, timestamp FROM chat_history WHERE username=? AND session_id=? ORDER BY id ASC LIMIT 200",
                (username, session_id)
            )
        else:
            rows = db.query_all(
                "SELECT sender, content, is_html, timestamp FROM chat_history WHERE username=? ORDER BY id ASC LIMIT 200",
                (username,)
            )
        return jsonify([{"sender": r['sender'], "content": r['content'], "is_html": r['is_html'], "timestamp": r['timestamp'] if r['timestamp'] else ""} for r in rows])
    except Exception as e: return jsonify({"error": str(e)}), 500

@bp.route("/api/chat/sessions", methods=["GET"])
@login_required
def list_sessions():
    username = session.get('username')
    try:
        # Ordena só por created_ts (epoch com microssegundos): não depende de
        # rowid implícito do SQLite, que não existe no PostgreSQL.
        rows = db.query_all(
            "SELECT id, created_at FROM chat_sessions WHERE username=? ORDER BY created_ts DESC",
            (username,)
        )
        return jsonify([{"id": r["id"], "created_at": r["created_at"] or ""} for r in rows])
    except Exception as e: return jsonify({"error": str(e)}), 500

@bp.route("/api/chat/new", methods=["POST"])
@login_required
def new_session():
    username = session.get('username')
    sid = str(uuid.uuid4())
    now = datetime.now()
    created_at = now.strftime("%d/%m/%Y %H:%M")
    try:
        db.execute(
            "INSERT INTO chat_sessions (id, username, created_at, created_ts) VALUES (?, ?, ?, ?)",
            (sid, username, created_at, now.timestamp())
        )
        return jsonify({"id": sid, "created_at": created_at})
    except Exception as e: return jsonify({"error": str(e)}), 500

@bp.route("/api/chat/delete_session", methods=["POST"])
@login_required
def delete_session():
    username = session.get('username')
    sid = (request.json or {}).get("id")
    if not sid: return jsonify({"error": "id ausente"}), 400
    try:
        db.execute("DELETE FROM chat_history WHERE username=? AND session_id=?", (username, sid))
        db.execute("DELETE FROM chat_sessions WHERE username=? AND id=?", (username, sid))
        return jsonify({"status": "deleted"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@bp.route("/api/chat/clear", methods=["POST"])
@login_required
def clear_history():
    username = session.get('username')
    try:
        db.execute("DELETE FROM chat_history WHERE username=?", (username,))
        db.execute("DELETE FROM chat_sessions WHERE username=?", (username,))
        return jsonify({"status": "cleared"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@bp.route('/api/active_scans', methods=['GET'])
@login_required
def get_active_scans():
    try:
        username = session.get('username')
        # Isolamento multi-tenant: cada usuário só enxerga os SEUS scans em execução.
        rows = db.query_all(
            "SELECT id, target, progress, stage, session_id FROM scans WHERE status='running' AND owner=?",
            (username,)
        )
        return jsonify(rows)
    except Exception as e:
        return jsonify([])

@bp.route('/api/download/<scan_id>', methods=['GET'])
@login_required
def download_report(scan_id):
    try:
        username = session.get('username')
        profile = str(session.get('profile') or '').upper().strip()
        row = db.query_one("SELECT target, result_json, owner FROM scans WHERE id = ?", (scan_id,))
        if not row or not row['result_json']: return "Relatório não disponível.", 404

        # Só o dono do scan (ou um administrador) pode baixar o relatório.
        owner = row['owner']
        if owner != username and profile not in ('ADMIN', 'ADMINISTRADOR'):
            return "Acesso negado.", 403

        target = row['target']
        ai_text = json.loads(row['result_json']).get("ai_analysis", "")
        
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
        {branding.get_logo_html()}
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
