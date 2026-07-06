import secrets
from werkzeug.security import generate_password_hash
from flask import Blueprint, request, redirect, url_for, session, render_template_string
from app import db

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
    return render_template_string(HTML_TEMPLATE, users=users)

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
