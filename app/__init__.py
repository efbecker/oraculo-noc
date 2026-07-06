import os
from flask import Flask
from flask_cors import CORS
from werkzeug.security import generate_password_hash
from config import SECRET_KEY
from app import db

def init_db():
    # Schema único e portável (SQLite/PostgreSQL) vem de app/db.py. Antes, esta
    # função criava um schema ANTIGO e divergente (role/zabbix_token/client_group/
    # chat_messages) que não batia com o schema real usado pelo resto do app
    # (profile/token_zabbix/grupo_cliente/chat_history). Unificado aqui.
    db.init_schema()

    # Cria o usuário Admin padrão se não existir nenhum usuário ainda.
    row = db.query_one("SELECT id FROM users WHERE username=?", ("admin",))
    if not row:
        hashed_pw = generate_password_hash("admin123")
        db.execute(
            "INSERT INTO users (username, password, profile, grupo_cliente, ai_model) VALUES (?, ?, ?, ?, ?)",
            ("admin", hashed_pw, "ADMINISTRADOR", "GLOBAL", "cohere.command-r-08-2024")
        )

    # Reseta scans travados caso o servidor tenha reiniciado com um scan em andamento.
    db.execute("UPDATE scans SET status='error', stage='Interrompido (Reboot)' WHERE status='running'")

def create_app():
    app = Flask(__name__, static_folder='static', static_url_path='/static')
    CORS(app)
    app.secret_key = SECRET_KEY
    
    # Inicializa Banco de Dados e Pastas
    init_db()
    os.makedirs(app.static_folder, exist_ok=True)

    # REGISTRO DOS BLUEPRINTS (A MÁGICA ACONTECE AQUI) ===
    from app.routes import auth, chat, admin, voice
    app.register_blueprint(auth.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(voice.bp)

    return app
