from functools import wraps
from flask import Blueprint, request, jsonify, session, redirect, url_for, render_template, render_template_string
from werkzeug.security import generate_password_hash, check_password_hash
from app import db
from app.services import branding

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


def admin_required(f):
    """Protege as páginas do painel Admin distinguindo DOIS casos que antes eram
    tratados igual (um seco "Acesso Negado." em texto puro):

      - NÃO LOGADO  -> manda para a tela de login (guardando a URL de destino,
                       para voltar direto ao painel depois de autenticar).
      - LOGADO, mas sem perfil de admin -> mostra uma página 403 decente, com
                       link de volta ao chat. É o único caso de "acesso negado"
                       de verdade.

    APIs (/admin/... chamadas via fetch) continuam recebendo JSON, para o
    JavaScript não tentar renderizar HTML de login dentro de um alerta."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            # Guarda o destino para voltar direto ao painel após autenticar.
            session['next_url'] = request.full_path.rstrip('?')
            return redirect(url_for('auth.login'))
        if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
            return render_template_string(PAGINA_ACESSO_NEGADO,
                                          usuario=session.get('username', '')), 403
        return f(*args, **kwargs)
    return decorated_function


PAGINA_ACESSO_NEGADO = """
<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">
<title>Acesso Negado — Oráculo</title>
<style>
  body { background:#121212; color:#e0e0e0; font-family:sans-serif; display:flex;
         align-items:center; justify-content:center; height:100vh; margin:0; }
  .box { background:#1e1e1e; border:1px solid #333; border-radius:10px; padding:40px;
         max-width:460px; text-align:center; }
  h1 { color:#ff9e00; margin:0 0 10px; font-size:22px; }
  p { color:#aaa; line-height:1.6; }
  a { display:inline-block; margin-top:18px; background:#00e5ff; color:#000;
      padding:10px 22px; border-radius:6px; text-decoration:none; font-weight:bold; }
</style></head><body>
  <div class="box">
    <h1>🔒 Acesso restrito</h1>
    <p>Olá, <strong>{{ usuario }}</strong>. Esta área é exclusiva para administradores
       e sua conta não tem essa permissão.</p>
    <p>Se você precisa de acesso, fale com o administrador do Oráculo.</p>
    <a href="/chat">← Voltar ao Chat</a>
  </div>
</body></html>
"""

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
                "SELECT id, password, profile, token_zabbix, grupo_cliente, zabbix_url FROM users WHERE username=?",
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

                # Se o usuário tentou abrir uma página protegida antes de logar
                # (ex.: /admin), volta direto pra lá em vez de cair sempre no chat.
                destino = session.pop('next_url', None) or '/chat'
                return jsonify({"status": "success", "redirect": destino})
            return jsonify({"error": "Credenciais inválidas"}), 401
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    return render_template('login.html', logo_data_uri=branding.get_logo_data_uri())

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
