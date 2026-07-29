import os
from flask import Flask, render_template_string
from flask_cors import CORS
from werkzeug.security import generate_password_hash
from app.settings import SECRET_KEY
from app import db

def init_db():
    # Schema único e portável (SQLite/PostgreSQL) vem de app/db.py. Antes, esta
    # função criava um schema ANTIGO e divergente (role/zabbix_token/client_group/
    # chat_messages) que não batia com o schema real usado pelo resto do app
    # (profile/token_zabbix/grupo_cliente/chat_history). Unificado aqui.
    db.init_schema()

    # Cria o usuário Admin padrão se não existir (função compartilhada com a
    # rota de troca de banco do painel, para nunca deixar um banco sem admin).
    db.garantir_admin_padrao()

    # Reseta scans travados caso o servidor tenha reiniciado com um scan em andamento.
    db.execute("UPDATE scans SET status='error', stage='Interrompido (Reboot)' WHERE status='running'")

def create_app():
    app = Flask(__name__, static_folder='static', static_url_path='/static')
    CORS(app)
    app.secret_key = SECRET_KEY
    
    # Inicializa Banco de Dados e Pastas
    init_db()
    os.makedirs(app.static_folder, exist_ok=True)

    # Agendador de backup (thread em background, uma vez por processo)
    from app.services import backup_manager
    backup_manager.start_scheduler()

    # REGISTRO DOS BLUEPRINTS (A MÁGICA ACONTECE AQUI) ===
    from app.routes import auth, chat, admin, logs_admin, veeam_admin
    app.register_blueprint(auth.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(logs_admin.bp)
    app.register_blueprint(veeam_admin.bp)

    # ---- Favicon (branding) -------------------------------------------------
    # Rota PÚBLICA (sem login): o navegador pede /favicon.ico em qualquer página,
    # inclusive na de login. Serve o favicon enviado no painel de Branding; se
    # não houver nenhum configurado, devolve 404 e o navegador usa o padrão.
    @app.route('/favicon.ico')
    def favicon():
        from flask import send_file, abort
        from app.services import branding
        caminho, mime = branding.get_favicon_arquivo()
        if not caminho:
            abort(404)
        resp = send_file(caminho, mimetype=mime)
        # Favicon muda raramente, mas queremos que uma troca no painel apareça
        # sem o usuário ter de limpar o cache.
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    # ---- Páginas de erro ----------------------------------------------------
    # URL inexistente (ex.: /adm, /painel, um link velho): se a pessoa NÃO está
    # logada, não faz sentido mostrar um 404 seco — o mais provável é que ela
    # queira entrar no sistema. Mandamos para o login. Se ESTÁ logada, aí sim
    # mostramos o 404, porque o problema é o endereço, não a sessão.
    @app.errorhandler(404)
    def pagina_nao_encontrada(e):
        from flask import session, redirect, url_for, request, jsonify
        if request.path.startswith('/api'):
            return jsonify({"error": "Endpoint não encontrado."}), 404
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return render_template_string(PAGINA_404), 404

    return app


PAGINA_404 = """
<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">
<title>Página não encontrada — Oráculo</title>
<style>
  body { background:#121212; color:#e0e0e0; font-family:sans-serif; display:flex;
         align-items:center; justify-content:center; height:100vh; margin:0; }
  .box { background:#1e1e1e; border:1px solid #333; border-radius:10px; padding:40px;
         max-width:460px; text-align:center; }
  h1 { color:#00e5ff; margin:0 0 10px; font-size:22px; }
  p { color:#aaa; line-height:1.6; }
  a { display:inline-block; margin-top:18px; background:#00e5ff; color:#000;
      padding:10px 22px; border-radius:6px; text-decoration:none; font-weight:bold; }
</style></head><body>
  <div class="box">
    <h1>🔮 Página não encontrada</h1>
    <p>O Oráculo não conhece este endereço.</p>
    <a href="/chat">← Voltar ao Chat</a>
  </div>
</body></html>
"""
