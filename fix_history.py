import sqlite3
from config import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
try:
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            sender TEXT NOT NULL,
            content TEXT NOT NULL,
            is_html INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    print("✅ Tabela 'chat_history' criada com sucesso! O histórico vai voltar a funcionar.")
except Exception as e:
    print("❌ Erro:", e)
finally:
    conn.close()
