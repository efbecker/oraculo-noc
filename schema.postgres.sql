-- ============================================================================
-- Oráculo NOC — Esquema do banco PostgreSQL
-- ============================================================================
-- ATENÇÃO: você normalmente NÃO precisa rodar este arquivo à mão.
-- A aplicação cria e migra todas as tabelas sozinha (db.init_schema()):
--   * automaticamente no boot; e
--   * ao clicar "Salvar e Aplicar" em Admin → Banco de Dados.
-- Basta criar o banco e o usuário vazios; a aplicação faz o resto.
--
-- Este arquivo existe para: (a) documentar o esquema no git; (b) permitir criar
-- as tabelas manualmente em revisões de DBA ou setups sem subir o app antes.
-- Ele espelha exatamente o init_schema() de app/db.py — se um dia divergirem,
-- o código é a fonte da verdade.
--
-- Uso manual (opcional):
--   sudo -u postgres psql -c "CREATE USER oraculo WITH PASSWORD 'SuaSenha';"
--   sudo -u postgres psql -c "CREATE DATABASE oraculo OWNER oraculo;"
--   psql -h HOST -U oraculo -d oraculo -f schema.postgres.sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS users (
    id             BIGSERIAL PRIMARY KEY,
    username       TEXT UNIQUE,
    password       TEXT,          -- hash (werkzeug), nunca texto puro
    profile        TEXT,          -- ADMINISTRADOR | CLIENTE
    grupo_cliente  TEXT,          -- grupo do Zabbix ao qual o usuário é restrito
    token_zabbix   TEXT,          -- token da API do Zabbix (por usuário)
    zabbix_url     TEXT,          -- URL específica do Zabbix (opcional; senão usa a global)
    ai_model       TEXT           -- legado: fallback de modelo p/ OCI (ver Admin → IA)
);

CREATE TABLE IF NOT EXISTS chat_history (
    id          BIGSERIAL PRIMARY KEY,
    username    TEXT,
    sender      TEXT,             -- 'user' | 'assistant'
    content     TEXT,
    is_html     INTEGER DEFAULT 0,
    timestamp   TEXT,
    session_id  TEXT
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id          TEXT PRIMARY KEY,
    username    TEXT,
    created_at  TEXT,
    created_ts  REAL
);

CREATE TABLE IF NOT EXISTS scans (
    id           TEXT PRIMARY KEY,
    target       TEXT,
    progress     REAL DEFAULT 0,
    stage        TEXT,
    status       TEXT,
    result_json  TEXT,
    owner        TEXT,
    session_id   TEXT
);

-- Índices das consultas mais frequentes.
CREATE INDEX IF NOT EXISTS idx_chat_hist_user_sess ON chat_history(username, session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user       ON chat_sessions(username, created_ts);
CREATE INDEX IF NOT EXISTS idx_scans_owner_status  ON scans(owner, status);

-- NOTA: a tabela de histórico de backups NÃO vive aqui — ela fica num SQLite
-- dedicado (app/services/backup_manager.py), separada de propósito para que
-- restaurar um backup não reverta o próprio histórico de backups.
