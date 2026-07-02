import sqlite3
from flask import Blueprint, request, redirect, url_for, session, render_template_string
from config import DB_PATH

admin_bp = Blueprint('admin', __name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <title>Painel Admin - Oráculo</title>
    <style>
        body { background-color: #121212; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 20px; }
        .container { max-width: 1200px; margin: auto; background: #1e1e1e; padding: 30px; border-radius: 8px; border: 1px solid #333; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; background: #161616; }
        th, td { border: 1px solid #333; padding: 12px; text-align: left; font-size: 14px; }
        th { background-color: #2a2a2a; color: #00e5ff; }
        input, select { background: #121212; color: white; border: 1px solid #444; padding: 8px; width: 95%; border-radius: 4px; }
        button { background-color: #00e5ff; color: #121212; border: none; padding: 8px 15px; cursor: pointer; font-weight: bold; border-radius: 4px; transition: 0.3s; }
        button:hover { background-color: #00b4d8; }
        .btn-danger { background-color: #ff4d4d; color: white; }
        .btn-danger:hover { background-color: #ff1a1a; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 15px; margin-bottom: 20px; }
        .logout { background-color: #ff4d4d; color: white; text-decoration: none; padding: 8px 15px; border-radius: 4px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1 style="color: #00e5ff; margin: 0;">⚙️ Gestão de Clientes (Multi-Tenant)</h1>
            <div>
                <a href="/chat" style="color: #00e5ff; margin-right: 20px; text-decoration: none; font-weight: bold;">💬 Voltar ao Chat</a>
                <a href="/logout" class="logout">Sair</a>
            </div>
        </div>
        
        <h3>Cadastrar / Atualizar Cliente</h3>
        <form method="POST" action="/admin/save">
            <table>
                <tr>
                    <th>Usuário (Login)</th>
                    <th>Senha</th>
                    <th>Perfil</th>
                    <th>Grupo Zabbix</th>
                    <th>Zabbix URL (Específica)</th>
                    <th>Token Zabbix</th>
                    <th>Ação</th>
                </tr>
                <tr>
                    <td><input type="text" name="username" placeholder="ex: pollux" required></td>
                    <td><input type="text" name="password" placeholder="Nova Senha"></td>
                    <td>
                        <select name="profile">
                            <option value="CLIENTE">CLIENTE</option>
                            <option value="NOC">NOC</option>
                            <option value="ADMIN">ADMINISTRADOR</option>
                        </select>
                    </td>
                    <td><input type="text" name="grupo_cliente" placeholder="VDC_POLLUX" required></td>
                    <td><input type="url" name="zabbix_url" placeholder="https://zabbix.cliente.com/api_jsonrpc.php"></td>
                    <td><input type="text" name="token_zabbix" placeholder="Token da API"></td>
                    <td><button type="submit">Salvar</button></td>
                </tr>
            </table>
        </form>

        <h3 style="margin-top: 40px;">Clientes Cadastrados no Oráculo</h3>
        <table>
            <tr>
                <th style="width: 50px;">ID</th>
                <th>Usuário</th>
                <th>Perfil</th>
                <th>Grupo Zabbix</th>
                <th>Zabbix URL Endpoint</th>
                <th style="width: 80px;">Ações</th>
            </tr>
            {% for u in users %}
            <tr>
                <td>{{ u[0] }}</td>
                <td><strong>{{ u[1] }}</strong></td>
                <td>{{ u[3] }}</td>
                <td><span style="color:#ff9e00; font-family: monospace;">{{ u[4] }}</span></td>
                <td><span style="color:#00e676; font-size: 12px;">{{ u[6] if u[6] else '🌍 Padrão Global (config.py)' }}</span></td>
                <td>
                    <form method="POST" action="/admin/delete" style="margin: 0;">
                        <input type="hidden" name="id" value="{{ u[0] }}">
                        <button type="submit" class="btn-danger">Excluir</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
        </table>
    </div>
</body>
</html>
"""

@admin_bp.route('/admin', methods=['GET'])
def index():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Puxa o campo zabbix_url (índice 6)
    c.execute("SELECT id, username, password, profile, grupo_cliente, token_zabbix, zabbix_url FROM users")
    users = c.fetchall()
    conn.close()
    
    return render_template_string(HTML_TEMPLATE, users=users)

@admin_bp.route('/admin/save', methods=['POST'])
def save_user():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
        
    username = request.form.get('username').strip()
    password = request.form.get('password')
    profile = request.form.get('profile')
    grupo_cliente = request.form.get('grupo_cliente').strip()
    token_zabbix = request.form.get('token_zabbix').strip()
    zabbix_url = request.form.get('zabbix_url').strip()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT id FROM users WHERE username=?", (username,))
    user = c.fetchone()
    
    if user:
        query = "UPDATE users SET profile=?, grupo_cliente=?, zabbix_url=?"
        params = [profile, grupo_cliente, zabbix_url]
        if password:
            query += ", password=?"
            params.append(password)
        if token_zabbix:
            query += ", token_zabbix=?"
            params.append(token_zabbix)
            
        query += " WHERE username=?"
        params.append(username)
        c.execute(query, tuple(params))
    else:
        c.execute("INSERT INTO users (username, password, profile, grupo_cliente, token_zabbix, zabbix_url, ai_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (username, password, profile, grupo_cliente, token_zabbix, zabbix_url, "cohere.command-r-08-2024"))
                  
    conn.commit()
    conn.close()
    return redirect(url_for('admin.index'))

@admin_bp.route('/admin/delete', methods=['POST'])
def delete_user():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    user_id = request.form.get('id')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin.index'))
