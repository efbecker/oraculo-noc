"""
Rotas do painel Admin para o módulo Veeam.

Ficam num BLUEPRINT separado (em vez de dentro do admin.py, que já é um arquivo
enorme) para reduzir risco de edição. A ABA visual (HTML) vive no template do
admin.py; estas são só as rotas JSON que os botões chamam via fetch(), no mesmo
padrão de /admin/ai/test e /admin/ai/apply.
"""
from flask import Blueprint, request, jsonify, session
from app.services import veeam

bp = Blueprint('veeam_admin', __name__)


def _admin_only():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return jsonify({"ok": False, "message": "Acesso negado."}), 403
    return None


@bp.route('/admin/veeam/test', methods=['POST'])
def veeam_test():
    guard = _admin_only()
    if guard:
        return guard
    data = request.json or {}
    ok, msg = veeam.test_connection(
        data.get('url'), user=data.get('user'),
        password=data.get('password'), verify_tls=bool(data.get('verify_tls')),
    )
    return jsonify({"ok": ok, "message": msg})


@bp.route('/admin/veeam/apply', methods=['POST'])
def veeam_apply():
    guard = _admin_only()
    if guard:
        return guard
    data = request.json or {}
    # Testa ANTES de aplicar — uma config quebrada não passa a valer.
    ok, msg = veeam.test_connection(
        data.get('url'), user=data.get('user'),
        password=data.get('password'), verify_tls=bool(data.get('verify_tls')),
    )
    if not ok:
        return jsonify({"ok": False, "message": f"Nada foi alterado — {msg}"})
    try:
        veeam.reconfigure(
            data.get('url'), user=data.get('user'),
            password=data.get('password'), verify_tls=bool(data.get('verify_tls')),
            persist=True,
        )
        return jsonify({"ok": True, "message": "✅ Conexão com o Veeam salva e aplicada. Vale já para as próximas mensagens."})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao aplicar: {e}"})


@bp.route('/admin/veeam/map/save', methods=['POST'])
def veeam_map_save():
    guard = _admin_only()
    if guard:
        return guard
    data = request.json or {}
    try:
        veeam.save_client_map(
            data.get('grupo'), mode=data.get('mode', 'prefix'), value=data.get('value'),
        )
        return jsonify({"ok": True, "message": "✅ Mapeamento salvo."})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro: {e}"})


@bp.route('/admin/veeam/map/delete', methods=['POST'])
def veeam_map_delete():
    guard = _admin_only()
    if guard:
        return guard
    data = request.json or {}
    veeam.delete_client_map((data.get('grupo') or '').strip())
    return jsonify({"ok": True, "message": "Removido."})
