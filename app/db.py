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
"""
Camada de acesso a banco UNIFICADA (SQLite <-> PostgreSQL).

Objetivo: migrar do SQLite para o PostgreSQL sem big-bang. Todo o app passa a
usar esta camada; o driver é escolhido, em ordem de prioridade:

    1) instance/db_config.json  (gravado pelo painel Admin > Banco de Dados)
    2) Variáveis de ambiente:    DB_ENGINE, PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD
    3) Padrão: sqlite (arquivo local), sem nenhuma configuração extra.

Isso permite uma instalação nova nascer em SQLite (com o admin padrão já
criado), e o próprio administrador apontar para um PostgreSQL pelo painel —
sem editar arquivo nenhum no servidor e SEM reiniciar o serviço.

Regras de portabilidade adotadas:
  - Placeholders sempre no estilo '?' nas queries do app; aqui traduzimos para
    '%s' quando o driver é PostgreSQL.
  - As funções query_all/query_one retornam SEMPRE lista de dicts, unificando o
    acesso por nome de coluna (row['coluna']) nos dois bancos.
  - O esquema é criado por init_schema(), com os poucos tipos que divergem
    (auto-incremento) tratados por driver.
"""
import os
import json
import threading
import contextlib
import sqlite3

try:
    from config import DB_PATH
except Exception:
    DB_PATH = os.environ.get('DB_PATH', 'oraculo_zabbix.db')

# Diretório de instância: onde ficam os arquivos de configuração GRAVADOS pelo
# próprio app (não vão para o git — ver .gitignore). Fica na raiz do projeto,
# um nível acima de app/.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
DB_CONFIG_FILE = os.path.join(INSTANCE_DIR, 'db_config.json')

_state_lock = threading.Lock()
_pg_pool = None


def _defaults_from_env():
    return {
        "engine": (os.environ.get('DB_ENGINE') or 'sqlite').strip().lower(),
        "host": os.environ.get('PG_HOST', '127.0.0.1'),
        "port": int(os.environ.get('PG_PORT', '5432') or 5432),
        "dbname": os.environ.get('PG_DB', 'oraculo'),
        "user": os.environ.get('PG_USER', 'oraculo'),
        "password": os.environ.get('PG_PASSWORD', ''),
        "pool_max": int(os.environ.get('PG_POOL_MAX', '10') or 10),
    }


def _load_state():
    """Carrega a config efetiva: arquivo (se existir) tem prioridade sobre env vars."""
    cfg = _defaults_from_env()
    try:
        if os.path.exists(DB_CONFIG_FILE):
            with open(DB_CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            cfg.update({k: v for k, v in saved.items() if v is not None})
    except Exception:
        pass  # arquivo corrompido/ilegível: segue só com env vars
    return cfg


_STATE = _load_state()


def is_postgres():
    return _STATE.get("engine") == "postgres"


def current_config(mask_password=True):
    """Config efetiva atual, para exibir no painel Admin (senha mascarada)."""
    cfg = dict(_STATE)
    if mask_password and cfg.get("password"):
        cfg["password"] = "•" * 8
    return cfg


def _pg_conn_kwargs(overrides=None):
    src = overrides or _STATE
    return dict(
        host=src.get("host"), port=int(src.get("port") or 5432),
        dbname=src.get("dbname"), user=src.get("user"), password=src.get("password") or "",
    )


def test_connection(engine, host=None, port=None, dbname=None, user=None, password=None, timeout=5):
    """Testa uma conexão SEM alterar o estado global (não mexe no pool ativo).
    Retorna (ok: bool, message: str). Usado pelo botão 'Testar Conexão'."""
    engine = (engine or "sqlite").strip().lower()
    if engine == "sqlite":
        return True, "SQLite não precisa de teste de rede (arquivo local)."
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=host, port=int(port or 5432), dbname=dbname, user=user,
            password=password or "", connect_timeout=timeout,
        )
        conn.close()
        return True, f"Conexão OK com '{dbname}' em {host}:{port}."
    except ImportError:
        return False, "Driver 'psycopg2' não instalado no servidor (pip install psycopg2-binary)."
    except Exception as e:
        return False, f"Falha na conexão: {e}"


def reconfigure(engine, host=None, port=None, dbname=None, user=None, password=None, persist=True):
    """Aplica uma nova configuração de banco EM TEMPO REAL (sem reiniciar o
    serviço): fecha o pool antigo (se havia) e a próxima query já usa a nova
    config. Se persist=True, grava em instance/db_config.json para sobreviver
    a um restart do serviço."""
    global _pg_pool
    engine = (engine or "sqlite").strip().lower()
    with _state_lock:
        _STATE["engine"] = engine
        if engine == "postgres":
            _STATE.update({
                "host": host, "port": int(port or 5432), "dbname": dbname,
                "user": user, "password": password or "",
            })
        if _pg_pool is not None:
            try:
                _pg_pool.closeall()
            except Exception:
                pass
            _pg_pool = None

        if persist:
            os.makedirs(INSTANCE_DIR, exist_ok=True)
            with open(DB_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(_STATE, f, indent=2)


def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        with _state_lock:
            if _pg_pool is None:
                from psycopg2 import pool as pgpool
                _pg_pool = pgpool.ThreadedConnectionPool(
                    1, int(_STATE.get("pool_max") or 10), **_pg_conn_kwargs()
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

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS backups (
                id {serial_pk},
                started_at TEXT,
                engine TEXT,
                status TEXT,
                size_bytes INTEGER,
                file_path TEXT,
                message TEXT
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_backups_started ON backups(started_at)")
