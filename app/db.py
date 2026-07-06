"""
Camada de acesso a banco UNIFICADA (SQLite <-> PostgreSQL).

Objetivo: migrar do SQLite para o PostgreSQL sem big-bang. Todo o app passa a
usar esta camada; o driver é escolhido por variável de ambiente:

    DB_ENGINE=sqlite     (padrão, comportamento atual)
    DB_ENGINE=postgres   (produção nova)

Para PostgreSQL, informe a conexão por variáveis de ambiente:
    PG_HOST, PG_PORT (5432), PG_DB, PG_USER, PG_PASSWORD
    PG_POOL_MAX (opcional, padrão 10)

Regras de portabilidade adotadas:
  - Placeholders sempre no estilo '?' nas queries do app; aqui traduzimos para
    '%s' quando o driver é PostgreSQL.
  - As funções query_all/query_one retornam SEMPRE lista de dicts, unificando o
    acesso por nome de coluna (row['coluna']) nos dois bancos.
  - O esquema é criado por init_schema(), com os poucos tipos que divergem
    (auto-incremento) tratados por driver.
"""
import os
import threading
import contextlib
import sqlite3

try:
    from config import DB_PATH
except Exception:
    DB_PATH = os.environ.get('DB_PATH', 'oraculo_zabbix.db')

ENGINE = (os.environ.get('DB_ENGINE') or 'sqlite').strip().lower()

_pg_pool = None
_pool_lock = threading.Lock()


def is_postgres():
    return ENGINE == 'postgres'


def _pg_conn_kwargs():
    return dict(
        host=os.environ.get('PG_HOST', '127.0.0.1'),
        port=int(os.environ.get('PG_PORT', '5432')),
        dbname=os.environ.get('PG_DB', 'oraculo'),
        user=os.environ.get('PG_USER', 'oraculo'),
        password=os.environ.get('PG_PASSWORD', ''),
    )


def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        with _pool_lock:
            if _pg_pool is None:
                from psycopg2 import pool as pgpool
                _pg_pool = pgpool.ThreadedConnectionPool(
                    1, int(os.environ.get('PG_POOL_MAX', '10')), **_pg_conn_kwargs()
                )
    return _pg_pool


def _translate(sql):
    # '?' (estilo SQLite) -> '%s' (psycopg) somente no PostgreSQL.
    return sql.replace('?', '%s') if is_postgres() else sql


@contextlib.contextmanager
def connection():
    """Fornece uma conexão com commit/rollback automáticos. No PostgreSQL usa
    pool de conexões (essencial com waitress multi-thread + threads de scan)."""
    if is_postgres():
        pool = _get_pg_pool()
        conn = pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=20)
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def execute(sql, params=()):
    """Executa um comando (INSERT/UPDATE/DELETE/DDL)."""
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(_translate(sql), params)


def executemany(sql, seq_params):
    with connection() as conn:
        cur = conn.cursor()
        cur.executemany(_translate(sql), seq_params)


def query_all(sql, params=()):
    """Retorna lista de dicts (acesso por nome de coluna nos dois bancos)."""
    with connection() as conn:
        cur = conn.cursor()
        cur.execute(_translate(sql), params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in rows]


def query_one(sql, params=()):
    rows = query_all(sql, params)
    return rows[0] if rows else None


def _column_exists(conn, table, column):
    cur = conn.cursor()
    if is_postgres():
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
            (table, column),
        )
        return cur.fetchone() is not None
    else:
        cur.execute(f"PRAGMA table_info({table})")
        return any(r[1] == column for r in cur.fetchall())


def _add_column_if_missing(conn, table, column, coltype):
    if not _column_exists(conn, table, column):
        conn.cursor().execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_schema():
    """Cria todas as tabelas do Oráculo de forma portável (SQLite/PostgreSQL) e
    garante as colunas adicionadas em versões posteriores. Substitui o
    init_db()/ensure_chat_schema()/init_scan_db() espalhados pelo código."""
    serial_pk = "BIGSERIAL PRIMARY KEY" if is_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with connection() as conn:
        cur = conn.cursor()

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                id {serial_pk},
                username TEXT UNIQUE,
                password TEXT,
                profile TEXT,
                grupo_cliente TEXT,
                token_zabbix TEXT,
                zabbix_url TEXT,
                ai_model TEXT
            )
        """)

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS chat_history (
                id {serial_pk},
                username TEXT,
                sender TEXT,
                content TEXT,
                is_html INTEGER DEFAULT 0,
                timestamp TEXT,
                session_id TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                username TEXT,
                created_at TEXT,
                created_ts REAL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                target TEXT,
                progress REAL DEFAULT 0,
                stage TEXT,
                status TEXT,
                result_json TEXT,
                owner TEXT,
                session_id TEXT
            )
        """)

        # Colunas adicionadas em versões posteriores (idempotente nos dois bancos).
        _add_column_if_missing(conn, 'chat_history', 'session_id', 'TEXT')
        _add_column_if_missing(conn, 'scans', 'owner', 'TEXT')
        _add_column_if_missing(conn, 'scans', 'session_id', 'TEXT')

        # Índices que ajudam as consultas mais quentes.
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_hist_user_sess ON chat_history(username, session_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON chat_sessions(username, created_ts)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_scans_owner_status ON scans(owner, status)")
