import os
import sqlite3
from flask import Flask
from flask_cors import CORS
from werkzeug.security import generate_password_hash
from config import DB_PATH, SECRET_KEY

def init_db():
    with sqlite3.connect(DB_PATH, timeout=15) as conn:
        # Tabela de Usuários Unificada (SecOps + Zabbix)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                zabbix_token TEXT,
                client_group TEXT,
                ai_provider TEXT DEFAULT 'oci',
                ai_api_key TEXT,
                ai_model TEXT DEFAULT 'cohere.command-r-16k'
            )
        ''')
        
        # Histórico de Chat e Áudio
        conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                sender TEXT,
                content TEXT,
                is_html INTEGER DEFAULT 0,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                audio_path TEXT
            )
        ''')
        
        # Testes de Penetração (Vuln::Hunter)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                timestamp TEXT,
                target TEXT,
                mode TEXT,
                status TEXT,
                progress REAL DEFAULT 0,
                stage TEXT,
                result_json TEXT,
                owner TEXT DEFAULT 'admin'
            )
        ''')
        
        # Configurações Globais
        conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                oci_model_id TEXT DEFAULT 'cohere.command-r-16k'
            )
        ''')
        
        # Cria o usuário Admin padrão se não existir
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE username='admin'")
        if not c.fetchone():
            hashed_pw = generate_password_hash('admin123')
            conn.execute(
                "INSERT INTO users (username, password, role, zabbix_token, client_group, ai_provider, ai_model) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                ('admin', hashed_pw, 'admin', '', 'GLOBAL', 'oci', 'cohere.command-r-16k')
            )
        
        # Reseta scans travados caso o servidor tenha reiniciado
        conn.execute("UPDATE scans SET status='error', stage='Interrompido (Reboot)' WHERE status='running'")
        conn.commit()

def create_app():
    app = Flask(__name__, static_folder='static', static_url_path='/static')
    CORS(app)
    app.secret_key = SECRET_KEY
    
    # Inicializa Banco de Dados e Pastas
    init_db()
    os.makedirs(app.static_folder, exist_ok=True)

    # REGISTRO DOS BLUEPRINTS (A MÁGICA ACONTECE AQUI) ===
    from app.routes import auth, chat, admin
    app.register_blueprint(auth.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(admin.bp)

    return app
