"""
Visualizador de Logs — menu próprio, separado do painel Admin principal (que já
tem bastante coisa: contas, banco de dados, backup). Admin-only.

Escaneia a pasta logs/ dinamicamente (não fixa nome de arquivo), então funciona
com qualquer *.log que a aplicação gere, sem depender do config.py real do
servidor (que não fica no repositório por conter segredos).
"""
import os
from flask import Blueprint, session, jsonify, request, send_file, render_template_string
from app import db

bp = Blueprint('logs_admin', __name__)

LOG_DIR = os.path.join(db.BASE_DIR, 'logs')


def _admin_guard_html():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    return None


def _admin_guard_json():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return jsonify({"ok": False, "message": "Acesso negado."}), 403
    return None


def _listar_arquivos():
    """Lista os *.log em LOG_DIR, mais recente primeiro. Não segue symlinks,
    não sai da pasta (evita qualquer risco de path traversal na origem)."""
    if not os.path.isdir(LOG_DIR):
        return []
    arquivos = []
    for nome in os.listdir(LOG_DIR):
        caminho = os.path.join(LOG_DIR, nome)
        if os.path.isfile(caminho) and not os.path.islink(caminho):
            arquivos.append({
                "name": nome,
                "size": os.path.getsize(caminho),
                "mtime": os.path.getmtime(caminho),
            })
    arquivos.sort(key=lambda a: a["mtime"], reverse=True)
    return arquivos


def _tail(path, n=500, max_bytes=3_000_000):
    """Lê as últimas N linhas de um arquivo grande sem carregar tudo na
    memória — lê em blocos a partir do fim até achar linhas suficientes."""
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        tamanho = f.tell()
        bloco = 4096
        dados = b''
        pos = tamanho
        while pos > 0 and dados.count(b'\n') <= n and (tamanho - pos) < max_bytes:
            passo = min(bloco, pos)
            pos -= passo
            f.seek(pos)
            dados = f.read(passo) + dados
        texto = dados.decode('utf-8', errors='replace')
        linhas = texto.splitlines()
        return linhas[-n:]


@bp.route('/admin/logs', methods=['GET'])
def logs_page():
    guard = _admin_guard_html()
    if guard: return guard
    return render_template_string(LOGS_TEMPLATE, arquivos=_listar_arquivos())


@bp.route('/admin/logs/files', methods=['GET'])
def logs_files():
    guard = _admin_guard_json()
    if guard: return guard
    return jsonify({"ok": True, "files": [a["name"] for a in _listar_arquivos()]})


@bp.route('/admin/logs/tail', methods=['GET'])
def logs_tail():
    guard = _admin_guard_json()
    if guard: return guard
    nome = request.args.get('file', '')
    try:
        n = max(50, min(5000, int(request.args.get('lines', 500))))
    except (TypeError, ValueError):
        n = 500

    validos = {a["name"] for a in _listar_arquivos()}
    if nome not in validos:
        return jsonify({"ok": False, "message": "Arquivo de log não encontrado."}), 404

    caminho = os.path.join(LOG_DIR, nome)
    try:
        linhas = _tail(caminho, n=n)
        return jsonify({
            "ok": True, "file": nome, "lines": linhas,
            "size": os.path.getsize(caminho), "mtime": os.path.getmtime(caminho),
        })
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao ler o log: {e}"}), 500


@bp.route('/admin/logs/download/<path:nome>', methods=['GET'])
def logs_download(nome):
    guard = _admin_guard_html()
    if guard: return guard
    validos = {a["name"] for a in _listar_arquivos()}
    if nome not in validos:
        return "Arquivo de log não encontrado.", 404
    return send_file(os.path.join(LOG_DIR, nome), as_attachment=True)


LOGS_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Logs - Oráculo</title>
    <style>
        body { background-color: #0b1117; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 15px; margin: 0; }
        .container { max-width: 1300px; margin: auto; background: #12181f; padding: 22px; border-radius: 8px; border: 1px solid #1f2933; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1f2933; padding-bottom: 15px; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
        .header h1 { color: #00e5ff; margin: 0; font-size: 22px; }
        .header a { color: #00e5ff; text-decoration: none; font-weight: bold; font-size: 13px; margin-left: 15px; }
        .toolbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 15px; }
        select, input { background: #0b1117; color: white; border: 1px solid #2d3748; padding: 8px 12px; border-radius: 4px; font-size: 13px; }
        .btn { background: #1f2933; color: #e0e0e0; border: 1px solid #2d3748; padding: 8px 14px; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: bold; }
        .btn:hover { background: #2d3748; }
        .btn.primary { background: #00e5ff; color: #0b1117; border-color: #00e5ff; }
        .checkbox-row { display: flex; align-items: center; gap: 6px; font-size: 13px; color: #aaa; }
        .meta { font-size: 12px; color: #666; margin-bottom: 10px; }
        #logBox { background: #05080b; border: 1px solid #1f2933; border-radius: 6px; padding: 15px; height: 65vh; overflow-y: auto; font-family: "SF Mono", Consolas, monospace; font-size: 12.5px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
        .l-error { color: #ff6b6b; }
        .l-warning { color: #ff9e00; }
        .l-info { color: #8ea3c4; }
        .l-default { color: #667; }
        .empty { color: #556; text-align: center; padding: 40px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📜 Logs do Sistema</h1>
            <div>
                <a href="/admin">⚙️ Painel Admin</a>
                <a href="/chat">💬 Voltar ao Chat</a>
                <a href="/logout" style="color:#ff4d4d;">Sair</a>
            </div>
        </div>

        {% if not arquivos %}
        <p class="empty">Nenhum arquivo .log encontrado em /logs.</p>
        {% else %}
        <div class="toolbar">
            <select id="fileSelect">
                {% for a in arquivos %}
                <option value="{{ a.name }}">{{ a.name }}</option>
                {% endfor %}
            </select>
            <select id="linesSelect">
                <option value="200">200 linhas</option>
                <option value="500" selected>500 linhas</option>
                <option value="1500">1500 linhas</option>
                <option value="5000">5000 linhas</option>
            </select>
            <input type="text" id="filterInput" placeholder="Filtrar (ex: ERROR, username)" style="min-width:220px;">
            <label class="checkbox-row"><input type="checkbox" id="autoRefresh" checked> Atualizar automaticamente</label>
            <button class="btn primary" onclick="carregarLog()">🔄 Atualizar agora</button>
            <button class="btn" onclick="baixarLog()">⬇️ Baixar arquivo</button>
        </div>
        <div class="meta" id="metaInfo"></div>
        <div id="logBox">Carregando...</div>
        {% endif %}
    </div>

    <script>
        let autoTimer = null;
        let ultimasLinhas = [];

        function corDaLinha(linha) {
            if (/\\|\\s*ERROR\\s*\\|/.test(linha)) return 'l-error';
            if (/\\|\\s*WARNING\\s*\\|/.test(linha)) return 'l-warning';
            if (/\\|\\s*INFO\\s*\\|/.test(linha)) return 'l-info';
            return 'l-default';
        }

        function renderLog() {
            const filtro = document.getElementById('filterInput').value.trim().toLowerCase();
            const box = document.getElementById('logBox');
            const linhasFiltradas = filtro ? ultimasLinhas.filter(l => l.toLowerCase().includes(filtro)) : ultimasLinhas;
            const estavaNoFim = (box.scrollTop + box.clientHeight) >= (box.scrollHeight - 40);

            box.innerHTML = linhasFiltradas.map(l => {
                const seguro = l.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                return `<div class="${corDaLinha(l)}">${seguro}</div>`;
            }).join('') || '<div class="empty">Nenhuma linha corresponde ao filtro.</div>';

            if (estavaNoFim) box.scrollTop = box.scrollHeight;
        }

        async function carregarLog() {
            const file = document.getElementById('fileSelect').value;
            const lines = document.getElementById('linesSelect').value;
            try {
                const r = await fetch(`/admin/logs/tail?file=${encodeURIComponent(file)}&lines=${lines}`);
                const data = await r.json();
                if (!data.ok) { document.getElementById('logBox').innerHTML = '<div class="empty">' + data.message + '</div>'; return; }
                ultimasLinhas = data.lines || [];
                renderLog();
                const kb = (data.size / 1024).toFixed(1);
                const dt = new Date(data.mtime * 1000).toLocaleString('pt-BR');
                document.getElementById('metaInfo').innerText = `${data.file} — ${kb} KB — última modificação: ${dt}`;
            } catch (e) {
                document.getElementById('logBox').innerHTML = '<div class="empty">Erro ao carregar o log.</div>';
            }
        }

        function baixarLog() {
            const file = document.getElementById('fileSelect').value;
            window.location.href = `/admin/logs/download/${encodeURIComponent(file)}`;
        }

        function reiniciarAutoRefresh() {
            if (autoTimer) clearInterval(autoTimer);
            if (document.getElementById('autoRefresh').checked) {
                autoTimer = setInterval(carregarLog, 4000);
            }
        }

        document.getElementById('fileSelect') && document.getElementById('fileSelect').addEventListener('change', carregarLog);
        document.getElementById('linesSelect') && document.getElementById('linesSelect').addEventListener('change', carregarLog);
        document.getElementById('filterInput') && document.getElementById('filterInput').addEventListener('input', renderLog);
        document.getElementById('autoRefresh') && document.getElementById('autoRefresh').addEventListener('change', reiniciarAutoRefresh);

        if (document.getElementById('fileSelect')) {
            carregarLog();
            reiniciarAutoRefresh();
        }
    </script>
</body>
</html>
"""
