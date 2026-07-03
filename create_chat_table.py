import sqlite3
from config import DB_PATH

def create_table():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                sender TEXT NOT NULL,
                content TEXT NOT NULL,
                is_html INTEGER DEFAULT 0,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        print("✅ Sucesso: Tabela 'chat_history' criada com perfeição!")
    except Exception as e:
        print("❌ Erro ao criar tabela:", e)
    finally:
        conn.close()

if __name__ == '__main__':
    create_table()
