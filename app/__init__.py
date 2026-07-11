import os
from flask import Flask
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
    from app.routes import auth, chat, admin, logs_admin
    app.register_blueprint(auth.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(logs_admin.bp)
    # Blueprint de voz (V2/OCI) desativado por enquanto — projeto pausado.
    # O arquivo app/routes/voice.py continua no repositório, só não é mais
    # registrado. Para retomar no futuro, reative as 2 linhas abaixo:
    # from app.routes import voice
    # app.register_blueprint(voice.bp)

    return app
