import sqlite3
from functools import wraps
from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from config import DB_PATH

bp = Blueprint('auth', __name__)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api'):
                return jsonify({"error": "Acesso não autorizado."}), 401
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

@bp.route("/login", methods=["GET", "POST"])
def login():
    if 'user_id' in session: 
        return redirect(url_for('chat.index'))
        
    if request.method == "POST":
        data = request.json
        username = data.get("username")
        password = data.get("password")
        try:
            with sqlite3.connect(DB_PATH, timeout=15) as conn:
                c = conn.cursor()
                # 1. CORREÇÃO: Buscando as colunas REAIS do banco novo Multi-Tenant
                c.execute("SELECT id, password, profile, token_zabbix, grupo_cliente, zabbix_url, ai_model FROM users WHERE username=?", (username,))
                user = c.fetchone()
                
                # 2. CORREÇÃO: Aceita a senha em texto puro (script) ou em Hash (segurança)
                if user and (user[1] == password or (user[1].startswith('pbkdf2:') and check_password_hash(user[1], password))):
                    session['user_id'] = user[0]
                    session['username'] = username
                    session['profile'] = user[2]
                    session['token_zabbix'] = user[3]
                    session['grupo_cliente'] = user[4]
                    session['zabbix_url'] = user[5]
                    session['ai_model'] = user[6]
                    return jsonify({"status": "ok"})
            return jsonify({"error": "Credenciais inválidas"}), 401
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    return render_template('login.html')

@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('auth.login'))

@bp.route("/api/users/me/password", methods=["PUT"])
@login_required
def change_my_password():
    data = request.json
    new_password = data.get("password", "")
    if len(new_password) < 4: 
        return jsonify({"error": "Senha muito curta."}), 400
    try:
        hashed_pw = generate_password_hash(new_password)
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.execute("UPDATE users SET password = ? WHERE id = ?", (hashed_pw, session['user_id']))
            conn.commit()
        return jsonify({"status": "success"})
    except Exception as e: 
        return jsonify({"error": str(e)}), 500
