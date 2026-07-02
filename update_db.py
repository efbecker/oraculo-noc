import sqlite3
from config import DB_PATH

def update_database():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE users ADD COLUMN zabbix_url TEXT DEFAULT ''")
        print("✅ Sucesso: Coluna 'zabbix_url' adicionada ao banco de dados!")
    except Exception as e:
        print("⚠️ Aviso (Pode ser que a coluna já exista):", e)
    
    conn.commit()
    conn.close()

if __name__ == '__main__':
    update_database()
