import os
import uuid
import json
import re
from datetime import datetime
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify, Response, send_file
from app.routes.auth import login_required
from app.services.ai_engine import process_oraculo_message
from app import db
from app.services import branding
from app.services import secops_render
from app.services import report_store

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
    from app.settings import SESSION_IDLE_MINUTES
    return render_template(
        'chat.html', 
        username=session.get('username'), 
        profile=session.get('profile'),
        grupo_cliente=session.get('grupo_cliente'),
        session_idle_minutes=SESSION_IDLE_MINUTES,
        cache_buster=str(__import__('time').time())
    )


@bp.route("/api/heartbeat", methods=["POST"])
@login_required
def heartbeat():
    """Pulso de atividade do navegador. Só é chamado pelo front quando há
    interação recente (mouse/teclado/scroll). Não faz nada além de existir: o
    before_request já renova o 'last_active' da sessão (esta rota NÃO está em
    POLL_PATHS). Serve para uma sessão ATIVA não expirar no servidor enquanto o
    usuário lê a tela sem clicar."""
    return ('', 204)

@bp.route("/api/chat/message", methods=["POST"])
@login_required
def send_message():
    data = request.json
    message = data.get("message", "").strip()
    
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
        message, username, token_zabbix, grupo_cliente,
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

@bp.route('/reports/<report_id>', methods=['GET'])
@login_required
def serve_finops_report(report_id):
    """Serve o relatório FinOps de forma AUTENTICADA e com checagem de tenant.
    Substitui o antigo link público /static/... : agora o arquivo vive em disco
    privado (instance/reports/) e só é entregue se o usuário logado pertencer ao
    mesmo grupo do relatório (ou for administrador)."""
    row = report_store.obter_relatorio(report_id)
    if not row:
        return "Relatório não disponível ou expirado.", 404

    # SecOps não tem arquivo próprio: o conteúdo vive em scans.result_json e é
    # servido pela rota já existente (/api/download/<scan_id>), que faz sua
    # própria checagem de dono/admin. Aqui só redirecionamos para ela.
    if row.get('tipo') == 'secops' and row.get('ref'):
        return redirect(url_for('chat.download_report', scan_id=row['ref']))

    if not row.get('caminho') or not os.path.isfile(row['caminho']):
        return "Relatório não disponível ou expirado.", 404

    if not report_store.usuario_pode_acessar(
        row,
        grupo_sessao=session.get('grupo_cliente'),
        perfil=session.get('profile'),
    ):
        return "Acesso negado.", 403

    # Inline (abre no navegador). O atributo download='' do botão no chat cuida
    # do caso em que o usuário prefere baixar o arquivo.
    nome = re.sub(r'[^A-Za-z0-9._-]+', '_', (row.get('titulo') or 'Relatorio')).strip('_') or 'Relatorio'
    return send_file(row['caminho'], mimetype='text/html', as_attachment=False,
                     download_name=f"{nome}.html")


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

        # Renderização Markdown → HTML de verdade (listas aninhadas, selos de
        # severidade, coloração de CVSS e links dos CVEs para o NVD). O conversor
        # antigo não tratava listas: os marcadores vazavam como asteriscos.
        html_content = secops_render.render_report_html(ai_text)
        
        # Mesmo tema escuro do relatório de VMs/FinOps (mesmas cores/classes),
        # pra manter os dois relatórios visualmente consistentes.
        html = f"""
        <html><head><meta charset="UTF-8"><title>Relatório SecOps - {target}</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#121212; color:#e0e0e0; margin:0; padding:20px; }}
            .container {{ max-width:900px; margin:auto; background:#161616; padding:40px; border-radius:12px; box-shadow:0 10px 40px rgba(0,0,0,0.8); border: 1px solid #2a2a2a; }}
            h1 {{ text-align:center; color:#00e5ff; margin-bottom:10px; font-size:32px; font-weight:700; letter-spacing: 1px; }}
            .report-info {{ text-align:center; color:#888; font-size:14px; margin-bottom:30px; padding-bottom:15px; border-bottom: 1px solid #333; }}
            .report-info b {{ color:#ccc; }}
            .content-box {{ background:#1c1c1c; border: 1px solid #2a2a2a; border-radius:8px; padding:25px 30px; line-height:1.7; font-size:15px; }}
            .content-box h1, .content-box h2, .content-box h3 {{ color:#00e5ff; margin-top:26px; margin-bottom:12px; }}
            .content-box h1:first-child, .content-box h2:first-child, .content-box h3:first-child {{ margin-top:0; }}
            .content-box b {{ color:#fff; }}
            .content-box a {{ color:#00e676; font-weight:bold; text-decoration:none; }}
            .content-box a:hover {{ text-decoration:underline; }}
            .footer-note {{ text-align:center; font-size:0.8em; color:#666; margin-top:35px; }}
            {secops_render.REPORT_EXTRA_CSS}
        </style>
        </head><body><div class="container">
        {branding.get_logo_html()}
        <h1>🛡️ Oráculo SecOps - Relatório Executivo</h1>
        <div class="report-info"><b>Alvo Auditado:</b> {target}<br><b>Data/Hora:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>
        <div class="content-box">{html_content}</div>
        <p class="footer-note">Gerado por Oráculo Armazem Cloud</p>
        </div></body></html>
        """
        
        return Response(
            html,
            mimetype="text/html",
            headers={"Content-disposition": f"attachment; filename=Relatorio_SecOps_{target}.html"}
        )
    except Exception as e:
        return f"Erro ao gerar relatório: {str(e)}", 500
