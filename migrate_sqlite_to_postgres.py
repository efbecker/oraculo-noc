"""
Migração ONE-SHOT de dados: copia o banco SQLite atual para o PostgreSQL.

Rode DEPOIS de provisionar o Postgres (ver SETUP_POSTGRES_OCI.md) e com as
variáveis de ambiente do Postgres exportadas. Exemplo:

    export DB_ENGINE=postgres
    export PG_HOST=10.0.0.20 PG_PORT=5432 PG_DB=oraculo PG_USER=oraculo PG_PASSWORD='...'
    export SQLITE_PATH=/opt/oraculo-noc/oraculo_zabbix.db
    python3 migrate_sqlite_to_postgres.py

O script:
  1. Cria o schema no Postgres (app/db.py -> init_schema).
  2. LIMPA as tabelas de destino (idempotente: pode rodar de novo sem duplicar).
  3. Copia users, chat_sessions, chat_history e scans, respeitando apenas as
     colunas que existem na origem (tolera schema antigo).
  4. Ajusta as sequences de id (users, chat_history) para o próximo valor certo.
"""
import os
import sys
import sqlite3

# Importa a camada de banco do app (em modo postgres, via env).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import db  # noqa: E402  (usa DB_ENGINE=postgres do ambiente)

TABELAS = {
    "users": ["id", "username", "password", "profile", "grupo_cliente", "token_zabbix", "zabbix_url", "ai_model"],
    "chat_sessions": ["id", "username", "created_at", "created_ts"],
    "chat_history": ["id", "username", "sender", "content", "is_html", "timestamp", "session_id"],
    "scans": ["id", "target", "progress", "stage", "status", "result_json", "owner", "session_id"],
}
SEQ_TABELAS = {"users": "users_id_seq", "chat_history": "chat_history_id_seq"}


def colunas_origem(scon, tabela):
    return [r[1] for r in scon.execute(f"PRAGMA table_info({tabela})").fetchall()]


def main():
    if not db.is_postgres():
        print("ERRO: defina DB_ENGINE=postgres (e as variáveis PG_*) antes de rodar.")
        sys.exit(1)

    sqlite_path = os.environ.get("SQLITE_PATH") or getattr(db, "DB_PATH", "oraculo_zabbix.db")
    if not os.path.exists(sqlite_path):
        print(f"ERRO: SQLite não encontrado em {sqlite_path}. Defina SQLITE_PATH.")
        sys.exit(1)

    print(f"Origem SQLite : {sqlite_path}")
    print(f"Destino PG    : {os.environ.get('PG_HOST')}:{os.environ.get('PG_PORT','5432')}/{os.environ.get('PG_DB')}")

    print("\n1) Criando schema no PostgreSQL...")
    db.init_schema()

    scon = sqlite3.connect(sqlite_path)
    scon.row_factory = sqlite3.Row

    total_geral = 0
    for tabela, cols_alvo in TABELAS.items():
        # tabela pode não existir na origem
        existe = scon.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tabela,)
        ).fetchone()
        if not existe:
            print(f"   - {tabela}: não existe na origem, pulando.")
            continue

        cols_orig = colunas_origem(scon, tabela)
        cols = [c for c in cols_alvo if c in cols_orig]  # interseção, na ordem do alvo
        if not cols:
            print(f"   - {tabela}: sem colunas compatíveis, pulando.")
            continue

        rows = scon.execute(f"SELECT {', '.join(cols)} FROM {tabela}").fetchall()

        # Limpa destino (idempotência) e copia
        db.execute(f"DELETE FROM {tabela}")
        if rows:
            placeholders = ", ".join(["?"] * len(cols))
            sql = f"INSERT INTO {tabela} ({', '.join(cols)}) VALUES ({placeholders})"
            db.executemany(sql, [tuple(r[c] for c in cols) for r in rows])

        print(f"   - {tabela}: {len(rows)} linha(s) copiada(s) (colunas: {', '.join(cols)}).")
        total_geral += len(rows)

    scon.close()

    # Ajusta as sequences de auto-incremento para não colidir com ids copiados.
    print("\n2) Ajustando sequences...")
    for tabela, seq in SEQ_TABELAS.items():
        try:
            db.execute(
                f"SELECT setval('{seq}', (SELECT COALESCE(MAX(id), 1) FROM {tabela}))"
            )
            print(f"   - {seq}: ajustada.")
        except Exception as e:
            print(f"   - {seq}: aviso ({e}).")

    print(f"\n✅ Migração concluída. {total_geral} linha(s) no total.")
    print("Agora suba o app com DB_ENGINE=postgres e valide o login/histórico.")


if __name__ == "__main__":
    main()
