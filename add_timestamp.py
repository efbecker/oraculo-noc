import sqlite3
from config import DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
try:
    c.execute("ALTER TABLE chat_history ADD COLUMN timestamp TEXT DEFAULT ''")
    conn.commit()
    print("✅ Coluna de data e hora adicionada com sucesso!")
except Exception as e:
    print("⚠️ Aviso (provavelmente a coluna já existe):", e)
finally:
    conn.close()
