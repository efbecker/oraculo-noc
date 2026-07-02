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
                c.execute("SELECT id, password, role, zabbix_token, client_group, ai_provider, ai_api_key, ai_model FROM users WHERE username=?", (username,))
                user = c.fetchone()
                
                if user and check_password_hash(user[1], password):
                    session['user_id'] = user[0]
                    session['username'] = username
                    session['role'] = user[2]
                    session['zabbix_token'] = user[3]
                    session['client_group'] = user[4]
                    session['ai_provider'] = user[5]
                    session['ai_api_key'] = user[6]
                    session['ai_model'] = user[7]
                    return jsonify({"status": "ok"})
            return jsonify({"error": "Credenciais inválidas"}), 401
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    # O Flask agora busca automaticamente na pasta 'app/templates'
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
