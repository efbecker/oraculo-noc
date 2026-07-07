import secrets
from werkzeug.security import generate_password_hash
from flask import Blueprint, request, redirect, url_for, session, render_template_string, jsonify, send_file
from app import db
from app.services import backup_manager

bp = Blueprint('admin', __name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Admin - Oráculo</title>
    <style>
        body { background-color: #121212; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 15px; margin: 0; }
        .container { max-width: 1200px; margin: auto; background: #1e1e1e; padding: 25px; border-radius: 8px; border: 1px solid #333; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 15px; margin-bottom: 25px; flex-wrap: wrap; gap: 15px; }
        .header h1 { color: #00e5ff; margin: 0; font-size: 24px; }
        
        /* Formulário em Grid Responsivo (Perfeito para Celular) */
        .form-container { background: #161616; padding: 20px; border-radius: 6px; border: 1px solid #333; margin-bottom: 30px; }
        .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .form-group { display: flex; flex-direction: column; gap: 6px; }
        .form-group label { font-size: 13px; color: #aaa; font-weight: bold; }
        
        input, select { background: #121212; color: white; border: 1px solid #444; padding: 10px; border-radius: 4px; font-size: 14px; width: 100%; box-sizing: border-box; }
        input:focus, select:focus { border-color: #00e5ff; outline: none; }
        
        /* Lista de Clientes Blindada para Telas Pequenas */
        .table-responsive { overflow-x: auto; background: #161616; border-radius: 6px; border: 1px solid #333; margin-top: 15px; }
        table { width: 100%; border-collapse: collapse; min-width: 900px; }
        th, td { border-bottom: 1px solid #2a2a2a; padding: 12px; text-align: left; font-size: 14px; }
        th { background-color: #242424; color: #00e5ff; font-weight: 600; text-transform: uppercase; font-size: 12px; letter-spacing: 0.5px; }
        tr:hover td { background-color: #1e1e1e; }
        
        .btn-submit { background-color: #00e5ff; color: #121212; border: none; padding: 12px 25px; cursor: pointer; font-weight: bold; border-radius: 4px; transition: 0.3s; font-size: 14px; width: auto; display: inline-block; }
        .btn-submit:hover { background-color: #00b4d8; }
        .btn-danger { background-color: #ff4d4d; color: white; border: none; padding: 6px 12px; cursor: pointer; border-radius: 4px; font-size: 12px; font-weight: bold; }
        .btn-danger:hover { background-color: #ff1a1a; }
        .btn-edit { background-color: #ff9e00; color: #121212; border: none; padding: 6px 12px; cursor: pointer; border-radius: 4px; font-size: 12px; font-weight: bold; text-decoration: none; }
        .btn-edit:hover { background-color: #e68a00; }
        .logout { background-color: #ff4d4d; color: white; text-decoration: none; padding: 8px 15px; border-radius: 4px; font-weight: bold; font-size: 13px; }
        .actions-cell { display: flex; gap: 8px; }
        .card-title { color: #ff9e00; margin-top: 0; margin-bottom: 18px; }
        .row-inline { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 10px; }
        .btn-secondary { background: #2a2a2a; color: #e0e0e0; border: 1px solid #444; padding: 10px 18px; border-radius: 4px; cursor: pointer; font-weight: bold; font-size: 13px; }
        .btn-secondary:hover { background: #333; }
        .status-msg { font-size: 13px; padding: 8px 12px; border-radius: 4px; margin-top: 10px; display: none; }
        .status-msg.ok { background: rgba(0,230,118,0.12); color: #00e676; display: block; }
        .status-msg.error { background: rgba(255,77,77,0.12); color: #ff6b6b; display: block; }
        .badge-engine { padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        .badge-engine.sqlite { background: rgba(255,158,0,0.15); color: #ff9e00; }
        .badge-engine.postgres { background: rgba(0,229,255,0.15); color: #00e5ff; }
        .badge-status { padding: 3px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; }
        .badge-status.ok { background: rgba(0,230,118,0.15); color: #00e676; }
        .badge-status.error { background: rgba(255,77,77,0.15); color: #ff6b6b; }
        .field-hint { font-size: 11px; color: #777; margin-top: 4px; }
        .checkbox-row { display: flex; align-items: center; gap: 8px; }
        .checkbox-row input { width: auto; }
        
        @media (max-width: 768px) {
            .header { flex-direction: column; align-items: flex-start; }
            .header div { width: 100%; display: flex; justify-content: space-between; margin-top: 5px; }
            .btn-submit { width: 100%; text-align: center; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>⚙️ Gestão de Clientes & Contas (Multi-Tenant)</h1>
            <div>
                <a href="/chat" style="color: #00e5ff; margin-right: 20px; text-decoration: none; font-weight: bold; font-size: 14px; align-self: center;">💬 Voltar ao Chat</a>
                <a href="/logout" class="logout">Sair</a>
            </div>
        </div>
        
        <div class="form-container">
            <h3 id="form-title" style="color: #ff9e00; margin-top: 0; margin-bottom: 20px;">Cadastrar / Atualizar Conta</h3>
            <form method="POST" action="/admin/save" id="userForm">
                <div class="form-grid">
                    <div class="form-group">
                        <label>Usuário (Login)</label>
                        <input type="text" name="username" id="form-username" placeholder="ex: pollux" required>
                    </div>
                    <div class="form-group">
                        <label>Senha</label>
                        <input type="text" name="password" id="form-password" placeholder="Senha (vazio p/ manter)">
                    </div>
                    <div class="form-group">
                        <label>Perfil de Acesso</label>
                        <select name="profile" id="form-profile">
                            <option value="CLIENTE">CLIENTE</option>
                            <option value="NOC">NOC</option>
                            <option value="ADMINISTRADOR">ADMINISTRADOR</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Grupo Zabbix</label>
                        <input type="text" name="grupo_cliente" id="form-grupo" placeholder="VDC_POLLUX" required>
                    </div>
                    <div class="form-group">
                        <label>Modelo de LLM (Oracle OCI)</label>
                        <input type="text" name="ai_model" id="form-model" placeholder="cohere.command-r-08-2024" required>
                    </div>
                    <div class="form-group">
                        <label>Zabbix URL (Específica)</label>
                        <input type="url" name="zabbix_url" id="form-url" placeholder="https://.../api_jsonrpc.php">
                    </div>
                    <div class="form-group">
                        <label>Token Zabbix</label>
                        <input type="text" name="token_zabbix" id="form-token" placeholder="Token da API">
                    </div>
                </div>
                <button type="submit" class="btn-submit" id="form-submit-btn">Salvar Configuração</button>
            </form>
        </div>

        <h3>Contas Cadastradas no Oráculo</h3>
        <div class="table-responsive">
            <table>
                <thead>
                    <tr>
                        <th style="width: 50px;">ID</th>
                        <th>Usuário</th>
                        <th>Perfil</th>
                        <th>Grupo Zabbix</th>
                        <th>Modelo LLM</th>
                        <th>Zabbix URL Endpoint</th>
                        <th style="width: 150px;">Ações</th>
                    </tr>
                </thead>
                <tbody>
                    {% for u in users %}
                    <tr>
                        <td>{{ u.id }}</td>
                        <td><strong>{{ u.username }}</strong></td>
                        <td>{{ u.profile }}</td>
                        <td><span style="color:#ff9e00; font-family: monospace;">{{ u.grupo_cliente }}</span></td>
                        <td><span style="color:#00e5ff; font-family: monospace; font-size: 13px;">{{ u.ai_model }}</span></td>
                        <td><span style="color:#00e676; font-size: 12px;">{{ u.zabbix_url if u.zabbix_url else '🌍 Padrão Global (config.py)' }}</span></td>
                        <td class="actions-cell">
                            <button type="button" class="btn-edit" onclick="editarUsuario('{{ u.username }}', '{{ u.profile }}', '{{ u.grupo_cliente }}', '{{ u.ai_model }}', '{{ u.zabbix_url }}', '{{ u.token_zabbix }}')">Editar</button>
                            <form method="POST" action="/admin/delete" style="margin: 0;" onsubmit="return confirm('Deseja excluir este usuário?');">
                                <input type="hidden" name="id" value="{{ u.id }}">
                                <button type="submit" class="btn-danger">Excluir</button>
                            </form>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

        <div class="form-container">
            <h3 class="card-title">🗄️ Banco de Dados</h3>
            <p style="font-size:13px; color:#aaa; margin-top:-10px;">
                Atual: <span class="badge-engine {{ db_cfg.engine }}">{{ 'PostgreSQL' if db_cfg.engine == 'postgres' else 'SQLite (local)' }}</span>
                {% if db_cfg.engine == 'postgres' %}— {{ db_cfg.host }}:{{ db_cfg.port }}/{{ db_cfg.dbname }}{% endif %}
            </p>
            <div class="form-grid">
                <div class="form-group">
                    <label>Motor</label>
                    <select id="db-engine" onchange="toggleDbFields()">
                        <option value="sqlite" {{ 'selected' if db_cfg.engine == 'sqlite' else '' }}>SQLite (arquivo local)</option>
                        <option value="postgres" {{ 'selected' if db_cfg.engine == 'postgres' else '' }}>PostgreSQL</option>
                    </select>
                </div>
                <div class="form-group db-pg-field">
                    <label>Host</label>
                    <input type="text" id="db-host" placeholder="10.0.0.230" value="{{ db_cfg.host if db_cfg.engine == 'postgres' else '' }}">
                </div>
                <div class="form-group db-pg-field">
                    <label>Porta</label>
                    <input type="text" id="db-port" placeholder="5432" value="{{ db_cfg.port if db_cfg.engine == 'postgres' else 5432 }}">
                </div>
                <div class="form-group db-pg-field">
                    <label>Nome do Banco</label>
                    <input type="text" id="db-name" placeholder="oraculo" value="{{ db_cfg.dbname if db_cfg.engine == 'postgres' else '' }}">
                </div>
                <div class="form-group db-pg-field">
                    <label>Usuário</label>
                    <input type="text" id="db-user" placeholder="oraculo" value="{{ db_cfg.user if db_cfg.engine == 'postgres' else '' }}">
                </div>
                <div class="form-group db-pg-field">
                    <label>Senha</label>
                    <input type="password" id="db-password" placeholder="Deixe em branco p/ manter a atual">
                </div>
            </div>
            <p class="field-hint db-pg-field">O banco e o usuário já devem existir no servidor PostgreSQL — este painel não cria banco novo nem usuário novo, só testa a conexão e cria/atualiza as tabelas do Oráculo dentro do banco informado.</p>
            <div class="row-inline">
                <button type="button" class="btn-secondary" onclick="testarConexaoDb()">🔌 Testar Conexão</button>
                <button type="button" class="btn-submit" onclick="aplicarConfigDb()">💾 Salvar e Aplicar</button>
            </div>
            <div id="db-status-msg" class="status-msg"></div>
        </div>

        <div class="form-container">
            <h3 class="card-title">💾 Backup Automático</h3>
            <div class="form-grid">
                <div class="form-group">
                    <div class="checkbox-row">
                        <input type="checkbox" id="bk-enabled" {{ 'checked' if backup_cfg.enabled else '' }}>
                        <label style="margin:0;">Backup automático ativado</label>
                    </div>
                </div>
                <div class="form-group">
                    <label>Frequência</label>
                    <select id="bk-frequency" onchange="toggleWeekdayField()">
                        <option value="daily" {{ 'selected' if backup_cfg.frequency == 'daily' else '' }}>Diário</option>
                        <option value="weekly" {{ 'selected' if backup_cfg.frequency == 'weekly' else '' }}>Semanal</option>
                    </select>
                </div>
                <div class="form-group" id="bk-weekday-field">
                    <label>Dia da semana</label>
                    <select id="bk-weekday">
                        <option value="0" {{ 'selected' if backup_cfg.weekday == 0 else '' }}>Segunda</option>
                        <option value="1" {{ 'selected' if backup_cfg.weekday == 1 else '' }}>Terça</option>
                        <option value="2" {{ 'selected' if backup_cfg.weekday == 2 else '' }}>Quarta</option>
                        <option value="3" {{ 'selected' if backup_cfg.weekday == 3 else '' }}>Quinta</option>
                        <option value="4" {{ 'selected' if backup_cfg.weekday == 4 else '' }}>Sexta</option>
                        <option value="5" {{ 'selected' if backup_cfg.weekday == 5 else '' }}>Sábado</option>
                        <option value="6" {{ 'selected' if backup_cfg.weekday == 6 else '' }}>Domingo</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Horário (servidor)</label>
                    <input type="text" id="bk-time" placeholder="02:00" value="{{ backup_cfg.time }}">
                </div>
                <div class="form-group">
                    <label>Reter últimos (nº de backups)</label>
                    <input type="text" id="bk-retention" placeholder="14" value="{{ backup_cfg.retention }}">
                </div>
            </div>
            <div class="row-inline">
                <button type="button" class="btn-submit" onclick="salvarConfigBackup()">💾 Salvar Agendamento</button>
                <button type="button" class="btn-secondary" onclick="rodarBackupAgora()">▶️ Rodar Backup Agora</button>
            </div>
            <div id="bk-status-msg" class="status-msg"></div>

            <h3 class="card-title" style="margin-top:30px; font-size:16px;">Histórico de Backups</h3>
            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>Data/Hora</th><th>Motor</th><th>Status</th><th>Tamanho</th><th>Mensagem</th><th style="width:100px;">Ação</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for b in backups %}
                        <tr>
                            <td>{{ b.started_at }}</td>
                            <td>{{ b.engine }}</td>
                            <td><span class="badge-status {{ b.status }}">{{ 'OK' if b.status == 'ok' else 'ERRO' }}</span></td>
                            <td>{{ '%.1f'|format(b.size_bytes / 1024 / 1024) + ' MB' if b.size_bytes else '-' }}</td>
                            <td style="max-width:280px; font-size:12px; color:#999;">{{ b.message }}</td>
                            <td>{% if b.status == 'ok' %}<a href="/admin/backup/download/{{ b.id }}" class="btn-edit" style="text-decoration:none;">Baixar</a>{% endif %}</td>
                        </tr>
                        {% else %}
                        <tr><td colspan="6" style="text-align:center; color:#666;">Nenhum backup executado ainda.</td></tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        function editarUsuario(username, profile, grupo, model, url, token) {
            document.getElementById('form-title').innerHTML = "📝 Editando Conta: <strong style='color:#00e5ff;'>" + username + "</strong>";
            document.getElementById('form-username').value = username;
            document.getElementById('form-username').readOnly = true;
            document.getElementById('form-profile').value = profile;
            document.getElementById('form-grupo').value = grupo;
            document.getElementById('form-model').value = model || 'cohere.command-r-08-2024';
            document.getElementById('form-url').value = url;
            document.getElementById('form-token').value = token;
            document.getElementById('form-password').placeholder = "Deixe em branco p/ não alterar";
            document.getElementById('form-submit-btn').innerText = "Atualizar Conta";
            window.scrollTo({top: 0, behavior: 'smooth'});
        }

        // ---------- Banco de Dados ----------
        function toggleDbFields() {
            const isPg = document.getElementById('db-engine').value === 'postgres';
            document.querySelectorAll('.db-pg-field').forEach(el => el.style.display = isPg ? '' : 'none');
        }
        function coletarDbForm() {
            return {
                engine: document.getElementById('db-engine').value,
                host: document.getElementById('db-host').value.trim(),
                port: document.getElementById('db-port').value.trim() || '5432',
                dbname: document.getElementById('db-name').value.trim(),
                user: document.getElementById('db-user').value.trim(),
                password: document.getElementById('db-password').value,
            };
        }
        function mostrarStatus(elId, ok, msg) {
            const el = document.getElementById(elId);
            el.className = 'status-msg ' + (ok ? 'ok' : 'error');
            el.innerText = msg;
        }
        async function testarConexaoDb() {
            const body = coletarDbForm();
            mostrarStatus('db-status-msg', true, '🔄 Testando conexão...');
            try {
                const r = await fetch('/admin/db/test', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
                const data = await r.json();
                mostrarStatus('db-status-msg', data.ok, data.message);
            } catch (e) { mostrarStatus('db-status-msg', false, 'Erro de comunicação: ' + e); }
        }
        async function aplicarConfigDb() {
            const body = coletarDbForm();
            if (body.engine === 'postgres' && !confirm('Isso troca o banco de dados em uso AGORA MESMO para todos os usuários. Continuar?')) return;
            mostrarStatus('db-status-msg', true, '🔄 Aplicando e criando tabelas...');
            try {
                const r = await fetch('/admin/db/apply', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
                const data = await r.json();
                mostrarStatus('db-status-msg', data.ok, data.message);
            } catch (e) { mostrarStatus('db-status-msg', false, 'Erro de comunicação: ' + e); }
        }

        // ---------- Backup ----------
        function toggleWeekdayField() {
            document.getElementById('bk-weekday-field').style.display = document.getElementById('bk-frequency').value === 'weekly' ? '' : 'none';
        }
        async function salvarConfigBackup() {
            const body = {
                enabled: document.getElementById('bk-enabled').checked,
                frequency: document.getElementById('bk-frequency').value,
                weekday: parseInt(document.getElementById('bk-weekday').value, 10),
                time: document.getElementById('bk-time').value.trim() || '02:00',
                retention: parseInt(document.getElementById('bk-retention').value, 10) || 14,
            };
            try {
                const r = await fetch('/admin/backup/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
                const data = await r.json();
                mostrarStatus('bk-status-msg', data.ok, data.message);
            } catch (e) { mostrarStatus('bk-status-msg', false, 'Erro de comunicação: ' + e); }
        }
        async function rodarBackupAgora() {
            mostrarStatus('bk-status-msg', true, '🔄 Rodando backup agora, aguarde...');
            try {
                const r = await fetch('/admin/backup/run_now', { method: 'POST' });
                const data = await r.json();
                mostrarStatus('bk-status-msg', data.ok, data.message);
                if (data.ok) setTimeout(() => window.location.reload(), 1500);
            } catch (e) { mostrarStatus('bk-status-msg', false, 'Erro de comunicação: ' + e); }
        }

        toggleDbFields();
        toggleWeekdayField();
    </script>
</body>
</html>
"""

@bp.route('/', methods=['GET'])
@bp.route('/admin', methods=['GET'])
def index():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    users = db.query_all("SELECT id, username, password, profile, grupo_cliente, token_zabbix, zabbix_url, ai_model FROM users")
    return render_template_string(
        HTML_TEMPLATE, users=users,
        db_cfg=db.current_config(),
        backup_cfg=backup_manager.load_config(),
        backups=backup_manager.historico(),
    )

@bp.route('/admin/save', methods=['POST'])
@bp.route('/save', methods=['POST'])
def save_user():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    username = request.form.get('username').strip()
    password = request.form.get('password')
    profile = request.form.get('profile')
    grupo_cliente = request.form.get('grupo_cliente').strip()
    token_zabbix = request.form.get('token_zabbix').strip()
    zabbix_url = request.form.get('zabbix_url').strip()
    ai_model = request.form.get('ai_model').strip()
    
    user = db.query_one("SELECT id FROM users WHERE username=?", (username,))
    
    if user:
        query = "UPDATE users SET profile=?, grupo_cliente=?, zabbix_url=?, ai_model=?"
        params = [profile, grupo_cliente, zabbix_url, ai_model]
        if password and password.strip() != "":
            query += ", password=?"
            params.append(generate_password_hash(password))  # nunca grava texto puro
        if token_zabbix:
            query += ", token_zabbix=?"
            params.append(token_zabbix)
        query += " WHERE username=?"
        params.append(username)
        db.execute(query, tuple(params))
    else:
        # Na criação: se vier senha, hasheia; se vier vazia, gera um hash aleatório
        # (conta fica sem login até definir a senha) — nunca armazena texto puro.
        if password and password.strip() != "":
            senha_hash = generate_password_hash(password)
        else:
            senha_hash = generate_password_hash(secrets.token_urlsafe(24))
        db.execute(
            "INSERT INTO users (username, password, profile, grupo_cliente, token_zabbix, zabbix_url, ai_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, senha_hash, profile, grupo_cliente, token_zabbix, zabbix_url, ai_model)
        )
    return redirect(request.referrer or url_for('admin.index'))

@bp.route('/admin/delete', methods=['POST'])
@bp.route('/delete', methods=['POST'])
def delete_user():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    user_id = request.form.get('id')
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    return redirect(request.referrer or url_for('admin.index'))


def _admin_only_json():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return jsonify({"ok": False, "message": "Acesso negado."}), 403
    return None


@bp.route('/admin/db/test', methods=['POST'])
def db_test():
    guard = _admin_only_json()
    if guard: return guard
    data = request.json or {}
    engine = (data.get('engine') or 'sqlite').strip().lower()

    # Senha em branco = "usar a senha já salva" (o campo nunca é preenchido de
    # volta por segurança, então testar sem digitar de novo deve usar a atual
    # em vez de mandar senha vazia — era exatamente isso que causava o
    # "password authentication failed" ao clicar Testar Conexão sem retypar).
    password = data.get('password')
    if engine == 'postgres' and not password:
        password = db.current_config(mask_password=False).get('password') or ''

    ok, msg = db.test_connection(
        engine, host=data.get('host'), port=data.get('port'),
        dbname=data.get('dbname'), user=data.get('user'), password=password
    )
    return jsonify({"ok": ok, "message": msg})


@bp.route('/admin/db/apply', methods=['POST'])
def db_apply():
    guard = _admin_only_json()
    if guard: return guard
    data = request.json or {}
    engine = (data.get('engine') or 'sqlite').strip().lower()

    # Senha em branco = "manter a atual" (evita apagar a senha salva ao só trocar outro campo).
    password = data.get('password')
    if engine == 'postgres' and not password:
        password = db.current_config(mask_password=False).get('password') or ''

    if engine == 'postgres':
        ok, msg = db.test_connection(
            engine, host=data.get('host'), port=data.get('port'),
            dbname=data.get('dbname'), user=data.get('user'), password=password
        )
        if not ok:
            return jsonify({"ok": False, "message": f"Conexão falhou, nada foi alterado: {msg}"})

    try:
        db.reconfigure(
            engine, host=data.get('host'), port=data.get('port'),
            dbname=data.get('dbname'), user=data.get('user'), password=password, persist=True
        )
        db.init_schema()
        return jsonify({"ok": True, "message": "✅ Configuração aplicada e tabelas verificadas/criadas com sucesso."})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao aplicar/criar tabelas: {e}"})


@bp.route('/admin/backup/config', methods=['POST'])
def backup_config_save():
    guard = _admin_only_json()
    if guard: return guard
    data = request.json or {}
    try:
        cfg = backup_manager.save_config({
            "enabled": bool(data.get('enabled')),
            "frequency": data.get('frequency') or 'daily',
            "weekday": int(data.get('weekday') or 0),
            "time": data.get('time') or '02:00',
            "retention": int(data.get('retention') or 14),
        })
        return jsonify({"ok": True, "message": "✅ Agendamento de backup salvo.", "config": cfg})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao salvar: {e}"})


@bp.route('/admin/backup/run_now', methods=['POST'])
def backup_run_now():
    guard = _admin_only_json()
    if guard: return guard
    resultado = backup_manager.run_backup_now()
    if resultado.get("status") == "ok":
        tamanho_mb = resultado.get("size", 0) / 1024 / 1024
        return jsonify({"ok": True, "message": f"✅ Backup concluído ({tamanho_mb:.1f} MB)."})
    return jsonify({"ok": False, "message": f"❌ {resultado.get('message', 'Falha desconhecida.')}"})


@bp.route('/admin/backup/download/<int:backup_id>', methods=['GET'])
def backup_download(backup_id):
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    row = db.query_one("SELECT file_path FROM backups WHERE id=? AND status='ok'", (backup_id,))
    if not row or not row.get('file_path') or not __import__('os').path.exists(row['file_path']):
        return "Arquivo de backup não encontrado.", 404
    return send_file(row['file_path'], as_attachment=True)
