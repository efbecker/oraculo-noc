import sqlite3
import os

DB_PATH = '/opt/oraculo-noc/oraculo_zabbix.db'

# Faz backup do banco antigo se existir
if os.path.exists(DB_PATH):
    os.rename(DB_PATH, DB_PATH + '.bkp_old')

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Cria a tabela definitiva com todas as colunas
c.execute('''
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        profile TEXT DEFAULT 'CLIENTE',
        grupo_cliente TEXT DEFAULT '',
        token_zabbix TEXT DEFAULT '',
        zabbix_url TEXT DEFAULT '',
        ai_model TEXT DEFAULT 'cohere.command-r-08-2024'
    )
''')

# Injeta o Administrador
c.execute("""
    INSERT INTO users (username, password, profile, grupo_cliente, token_zabbix, zabbix_url)
    VALUES ('admin', 'admin', 'ADMINISTRADOR', 'GLOBAL', '', '')
""")

# Injeta a Pollux
c.execute("""
    INSERT INTO users (username, password, profile, grupo_cliente, token_zabbix, zabbix_url)
    VALUES ('pollux', 'pollux123', 'CLIENTE', 'VDC_POLLUX', 'TOKEN_POLLUX_AQUI', 'URL_POLLUX_AQUI')
""")

# Injeta a Akato
c.execute("""
    INSERT INTO users (username, password, profile, grupo_cliente, token_zabbix, zabbix_url)
    VALUES ('akato', 'akato123', 'CLIENTE', 'VDC_AKATO', 'TOKEN_AKATO_AQUI', 'URL_AKATO_AQUI')
""")

conn.commit()
conn.close()
print("✅ Banco de dados RECRIADO com sucesso! Tabela Multi-Tenant perfeita.")
