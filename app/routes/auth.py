from functools import wraps
from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template
from werkzeug.security import generate_password_hash, check_password_hash
from app import db

bp = Blueprint('auth', __name__)

def _is_hashed(pw):
    """Detecta se a senha armazenada já é um hash do Werkzeug
    (scrypt, pbkdf2 ou argon2) em vez de texto puro."""
    if not isinstance(pw, str) or "$" not in pw:
        return False
    metodo = pw.split("$", 1)[0].split(":", 1)[0].lower()
    return metodo in ("pbkdf2", "scrypt", "argon2", "argon2id")

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
            user = db.query_one(
                "SELECT id, password, profile, token_zabbix, grupo_cliente, zabbix_url, ai_model FROM users WHERE username=?",
                (username,)
            )

            auth_ok = False
            if user and password:
                stored = user['password'] or ""
                if _is_hashed(stored):
                    # Senha já protegida: verificação criptográfica (scrypt/pbkdf2/argon2).
                    try:
                        auth_ok = check_password_hash(stored, password)
                    except Exception:
                        auth_ok = False
                else:
                    # LEGADO em texto puro: valida uma única vez e migra para hash
                    # imediatamente, para não trancar ninguém durante a transição.
                    # >>> Depois de migrar toda a base, este 'else' pode ser removido. <<<
                    if stored and stored == password:
                        auth_ok = True
                        try:
                            db.execute("UPDATE users SET password=? WHERE id=?",
                                       (generate_password_hash(password), user['id']))
                        except Exception:
                            pass

            if auth_ok:
                session['user_id'] = user['id']
                session['username'] = username
                session['profile'] = user['profile']
                session['token_zabbix'] = user['token_zabbix']
                session['grupo_cliente'] = user['grupo_cliente']
                session['zabbix_url'] = user['zabbix_url']
                session['ai_model'] = user['ai_model']
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
        db.execute("UPDATE users SET password = ? WHERE id = ?", (hashed_pw, session['user_id']))
        return jsonify({"status": "success"})
    except Exception as e: 
        return jsonify({"error": str(e)}), 500
