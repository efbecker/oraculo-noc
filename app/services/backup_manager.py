"""
Motor de backup do Oráculo — funciona com o banco que estiver ativo (SQLite ou
PostgreSQL), sem depender de cron/systemd (útil também em container). Guarda:

  - Configuração do agendamento em instance/backup_config.json
  - Arquivos de backup em instance/backups/ (disco local da VM)
  - Histórico na tabela 'backups' (para exibir no painel Admin)

Agendamento: um thread simples que acorda a cada minuto e verifica se é hora
de rodar (dia da semana + HH:MM configurados), evitando rodar duas vezes no
mesmo minuto. Não depende de nenhuma lib externa.
"""
import os
import io
import json
import time
import uuid
import shutil
import sqlite3
import logging
import threading
import subprocess
from datetime import datetime

from app import db

logger = logging.getLogger('Oraculo')

BACKUP_DIR = os.path.join(db.INSTANCE_DIR, 'backups')
BACKUP_CONFIG_FILE = os.path.join(db.INSTANCE_DIR, 'backup_config.json')

# Histórico de backup/restore fica num SQLite DEDICADO, sempre local, SEPARADO
# do banco principal (seja ele SQLite ou PostgreSQL) — mesmo se o banco
# principal for o Postgres. Motivo: restaurar o banco principal não pode
# apagar/reverter o próprio registro de que a restauração aconteceu.
META_DB_PATH = os.path.join(db.INSTANCE_DIR, 'backup_meta.db')

DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]  # 0=segunda

_DEFAULT_CONFIG = {
    "enabled": False,
    "frequency": "daily",   # "daily" | "weekly"
    "weekday": 0,           # usado só se frequency == "weekly" (0=segunda)
    "time": "02:00",        # HH:MM, horário local do servidor
    "retention": 14,        # quantos backups manter (os mais antigos são apagados)
}

_sched_thread = None
_sched_stop = threading.Event()
_last_run_minute_key = None  # evita disparo duplicado dentro do mesmo minuto


def _meta_conn():
    os.makedirs(db.INSTANCE_DIR, exist_ok=True)
    conn = sqlite3.connect(META_DB_PATH, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _ensure_meta_schema():
    with _meta_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                engine TEXT,
                status TEXT,
                size_bytes INTEGER,
                file_path TEXT,
                message TEXT,
                kind TEXT DEFAULT 'backup'
            )
        """)
        conn.commit()

    # Migração de cortesia (uma vez só): versões anteriores guardavam esse
    # histórico dentro do banco principal. Se o store dedicado estiver vazio
    # e existir histórico antigo lá, copia para não perder o que já existia.
    try:
        with _meta_conn() as conn:
            ja_tem = conn.execute("SELECT COUNT(*) FROM backups").fetchone()[0]
        if ja_tem == 0:
            antigos = db.query_all(
                "SELECT started_at, engine, status, size_bytes, file_path, message FROM backups ORDER BY id ASC"
            )
            if antigos:
                with _meta_conn() as conn:
                    conn.executemany(
                        "INSERT INTO backups (started_at, engine, status, size_bytes, file_path, message, kind) "
                        "VALUES (?,?,?,?,?,?,'backup')",
                        [(r['started_at'], r['engine'], r['status'], r['size_bytes'], r['file_path'], r['message']) for r in antigos]
                    )
                    conn.commit()
                logger.info(f"[Backup] Migrados {len(antigos)} registro(s) de histórico antigo para o store dedicado.")
    except Exception:
        pass  # tabela antiga pode nem existir (instalação nova) — normal, ignora

_ensure_meta_schema()


def load_config():
    cfg = dict(_DEFAULT_CONFIG)
    try:
        if os.path.exists(BACKUP_CONFIG_FILE):
            with open(BACKUP_CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
    except Exception as e:
        logger.warning(f"[Backup] Falha ao ler config, usando padrão: {e}")
    return cfg


def save_config(cfg):
    os.makedirs(db.INSTANCE_DIR, exist_ok=True)
    full = dict(_DEFAULT_CONFIG)
    full.update(cfg or {})
    with open(BACKUP_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(full, f, indent=2)
    return full


def _registrar(started_at, engine, status, size_bytes, file_path, message, kind="backup"):
    try:
        with _meta_conn() as conn:
            conn.execute(
                "INSERT INTO backups (started_at, engine, status, size_bytes, file_path, message, kind) VALUES (?,?,?,?,?,?,?)",
                (started_at, engine, status, size_bytes, file_path, message, kind)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"[Backup] Falha ao registrar histórico: {e}")


def _aplicar_retencao(retention):
    """Mantém só os N backups mais recentes em disco (apaga o arquivo mais
    antigo além do limite). O histórico no banco NÃO é apagado — só o arquivo."""
    try:
        with _meta_conn() as conn:
            rows = conn.execute(
                "SELECT id, file_path FROM backups WHERE status='ok' AND kind='backup' ORDER BY id DESC"
            ).fetchall()
        for _id, fp in rows[retention:]:
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[Backup] Falha ao aplicar retenção: {e}")


def run_backup_now():
    """Executa um backup imediatamente (chamado pelo agendador OU pelo botão
    'Rodar agora' do painel). Retorna dict com o resultado."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    started_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    # Microssegundos + sufixo aleatório: garante que o nome do arquivo NUNCA
    # colide, mesmo quando dois backups acontecem no mesmo segundo (ex.: o
    # backup de segurança automático disparado logo antes de uma restauração).
    # Uma colisão de nome faria um backup SOBRESCREVER o outro silenciosamente.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
    engine = "postgres" if db.is_postgres() else "sqlite"

    try:
        if engine == "sqlite":
            dest = os.path.join(BACKUP_DIR, f"oraculo_sqlite_{stamp}.db")
            # Usa a API de backup do sqlite3 (segura com o banco em uso — não é
            # uma simples cópia de arquivo, que arriscaria pegar o .db no meio
            # de uma escrita).
            src_conn = sqlite3.connect(db.DB_PATH)
            dst_conn = sqlite3.connect(dest)
            with dst_conn:
                src_conn.backup(dst_conn)
            src_conn.close()
            dst_conn.close()
            size = os.path.getsize(dest)
            _registrar(started_at, engine, "ok", size, dest, "Backup SQLite concluído.")
            resultado = {"status": "ok", "file": dest, "size": size}

        else:
            cfg = db._pg_conn_kwargs()
            dest = os.path.join(BACKUP_DIR, f"oraculo_postgres_{stamp}.sql.gz")
            env = os.environ.copy()
            env["PGPASSWORD"] = cfg.get("password") or ""
            cmd = [
                "pg_dump", "-h", str(cfg["host"]), "-p", str(cfg["port"]),
                "-U", cfg["user"], "-d", cfg["dbname"], "--no-password",
                "--clean", "--if-exists",  # dump inclui DROP ... IF EXISTS antes de cada CREATE,
                                            # o que torna a restauração IDEMPOTENTE (pode restaurar
                                            # por cima de um banco que já tem as tabelas, sem erro
                                            # de "relation already exists").
            ]
            with open(dest, "wb") as fdest:
                import gzip
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                with gzip.GzipFile(fileobj=fdest, mode="wb") as gz:
                    while True:
                        chunk = proc.stdout.read(65536)
                        if not chunk:
                            break
                        gz.write(chunk)
                _, stderr = proc.communicate(timeout=300)
            if proc.returncode != 0:
                erro = stderr.decode(errors="ignore")[:500]
                if os.path.exists(dest):
                    os.remove(dest)
                _registrar(started_at, engine, "error", 0, None, f"pg_dump falhou: {erro}")
                return {"status": "error", "message": f"pg_dump falhou: {erro}"}
            size = os.path.getsize(dest)
            _registrar(started_at, engine, "ok", size, dest, "Backup PostgreSQL (pg_dump) concluído.")
            resultado = {"status": "ok", "file": dest, "size": size}

        cfg_backup = load_config()
        _aplicar_retencao(int(cfg_backup.get("retention") or 14))
        return resultado

    except FileNotFoundError:
        msg = "Binário 'pg_dump' não encontrado no servidor (apt install postgresql-client)."
        _registrar(started_at, engine, "error", 0, None, msg)
        return {"status": "error", "message": msg}
    except Exception as e:
        logger.error(f"[Backup] Falha no backup: {e}")
        _registrar(started_at, engine, "error", 0, None, str(e))
        return {"status": "error", "message": str(e)}


def historico(limit=30):
    with _meta_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, started_at, engine, status, size_bytes, file_path, message, kind FROM backups ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_backup(backup_id):
    with _meta_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, started_at, engine, status, size_bytes, file_path, message, kind FROM backups WHERE id=?",
            (backup_id,)
        ).fetchone()
        return dict(row) if row else None


def _deve_rodar_agora(cfg, agora):
    hhmm = agora.strftime("%H:%M")
    if hhmm != cfg.get("time", "02:00"):
        return False
    if cfg.get("frequency") == "weekly":
        return agora.weekday() == int(cfg.get("weekday", 0))
    return True  # daily


def _loop_agendador():
    global _last_run_minute_key
    logger.info("[Backup] Agendador iniciado.")
    while not _sched_stop.wait(30):
        try:
            cfg = load_config()
            if not cfg.get("enabled"):
                continue
            agora = datetime.now()
            minute_key = agora.strftime("%Y-%m-%d %H:%M")
            if minute_key == _last_run_minute_key:
                continue  # já rodou neste minuto
            if _deve_rodar_agora(cfg, agora):
                _last_run_minute_key = minute_key
                logger.info("[Backup] Disparando backup agendado...")
                run_backup_now()
        except Exception as e:
            logger.error(f"[Backup] Erro no loop do agendador: {e}")


def start_scheduler():
    """Inicia o agendador em background (uma única vez por processo)."""
    global _sched_thread
    if _sched_thread is not None:
        return
    _sched_stop.clear()
    _sched_thread = threading.Thread(target=_loop_agendador, daemon=True)
    _sched_thread.start()


# ==========================================================================
# RESTAURAÇÃO — operação DESTRUTIVA: substitui os dados atuais pelos do
# backup. Sempre tira um "backup de segurança" do estado atual ANTES de
# restaurar, para permitir desfazer se algo der errado.
# ==========================================================================

def _is_gzip(path):
    try:
        with open(path, 'rb') as f:
            return f.read(2) == b'\x1f\x8b'
    except Exception:
        return False


def restore_from_file(path, origem_label, backup_engine_hint=None):
    """Restaura o banco ATIVO a partir de um arquivo de backup.
    - path: caminho do arquivo (.db do SQLite, ou .sql/.sql.gz do PostgreSQL)
    - origem_label: texto para o histórico (ex.: 'backup #14' ou 'upload: x.sql.gz')
    - backup_engine_hint: engine com que o backup foi gerado (se soubermos, ex.
      vindo do histórico); usado só para checar compatibilidade antes de tentar.
    Retorna dict {"status": "ok"|"error", "message": str}.
    """
    if not os.path.exists(path):
        return {"status": "error", "message": "Arquivo de backup não encontrado."}

    engine_atual = "postgres" if db.is_postgres() else "sqlite"
    if backup_engine_hint and backup_engine_hint != engine_atual:
        return {
            "status": "error",
            "message": f"Este backup foi gerado em {backup_engine_hint.upper()}, mas o sistema está "
                       f"configurado para {engine_atual.upper()} agora. Ajuste o motor em 'Banco de "
                       f"Dados' antes de restaurar, ou escolha outro arquivo."
        }

    # Defesa em profundidade: isola o arquivo de origem numa cópia privada
    # ANTES de disparar o backup de segurança (próximo passo). Sem isso, se o
    # backup de segurança colidisse de nome com o arquivo de origem, ele
    # sobrescreveria os dados que estamos prestes a restaurar.
    os.makedirs(BACKUP_DIR, exist_ok=True)
    staging_path = os.path.join(BACKUP_DIR, f"_restore_staging_{uuid.uuid4().hex}{os.path.splitext(path)[1]}")
    shutil.copyfile(path, staging_path)

    started_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    # 1) Backup de SEGURANÇA do estado atual, antes de mexer em qualquer coisa.
    #    Best-effort: se o banco atual já estiver quebrado, isso pode falhar —
    #    registramos o aviso mas seguimos com a restauração mesmo assim, já
    #    que foi exatamente para esse cenário de emergência que o botão existe.
    seguranca_ok = True
    try:
        resultado_seguranca = run_backup_now()
        if resultado_seguranca.get("status") != "ok":
            seguranca_ok = False
    except Exception:
        seguranca_ok = False

    aviso_seguranca = "" if seguranca_ok else " ⚠️ Não foi possível confirmar o backup de segurança automático antes de restaurar."

    try:
        if engine_atual == "sqlite":
            resultado = _restore_sqlite(staging_path)
        else:
            resultado = _restore_postgres(staging_path)
    except Exception as e:
        logger.error(f"[Restore] Falha: {e}")
        resultado = {"status": "error", "message": str(e)}
    finally:
        if os.path.exists(staging_path):
            try:
                os.remove(staging_path)
            except Exception:
                pass

    status = resultado.get("status")
    msg_final = resultado.get("message", "") + aviso_seguranca
    _registrar(started_at, engine_atual, status, os.path.getsize(path) if os.path.exists(path) else 0,
               path, f"Restaurado a partir de {origem_label}. {msg_final}".strip(), kind="restore")
    return {"status": status, "message": msg_final or ("Restauração concluída." if status == "ok" else "Falha na restauração.")}


def _restore_sqlite(path):
    # Valida que o arquivo é mesmo um banco SQLite legível antes de sobrescrever
    # o atual (evita destruir os dados de produção com um arquivo inválido).
    try:
        test_conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        test_conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        test_conn.close()
    except Exception as e:
        return {"status": "error", "message": f"Arquivo não é um banco SQLite válido: {e}"}

    # Checkpoint no banco atual (esvazia o WAL) antes de trocar o arquivo.
    try:
        cur_conn = sqlite3.connect(db.DB_PATH, timeout=10)
        cur_conn.execute("PRAGMA wal_checkpoint(FULL)")
        cur_conn.close()
    except Exception:
        pass

    tmp_dest = db.DB_PATH + ".restoring_tmp"
    shutil.copyfile(path, tmp_dest)
    os.replace(tmp_dest, db.DB_PATH)  # troca atômica

    # Remove WAL/SHM órfãos do banco anterior (o arquivo novo começa "limpo").
    for suf in ("-wal", "-shm"):
        stray = db.DB_PATH + suf
        if os.path.exists(stray):
            try:
                os.remove(stray)
            except Exception:
                pass

    return {"status": "ok", "message": "Banco SQLite restaurado com sucesso."}


def _restore_postgres(path):
    sql_path = path
    tmp_decompressed = None
    if _is_gzip(path):
        import gzip
        tmp_decompressed = path + ".restoring.sql"
        with gzip.open(path, 'rb') as fin, open(tmp_decompressed, 'wb') as fout:
            shutil.copyfileobj(fin, fout)
        sql_path = tmp_decompressed

    cfg = db._pg_conn_kwargs()
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg.get("password") or ""
    cmd = [
        "psql", "-h", str(cfg["host"]), "-p", str(cfg["port"]),
        "-U", cfg["user"], "-d", cfg["dbname"], "--no-password",
        "-v", "ON_ERROR_STOP=1", "-f", sql_path,
    ]
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=600)
    except FileNotFoundError:
        return {"status": "error", "message": "Binário 'psql' não encontrado no servidor (apt install postgresql-client)."}
    finally:
        if tmp_decompressed and os.path.exists(tmp_decompressed):
            try:
                os.remove(tmp_decompressed)
            except Exception:
                pass

    if proc.returncode != 0:
        erro = proc.stderr.decode(errors="ignore")[:800]
        return {"status": "error", "message": f"psql retornou erro (a restauração pode ter ficado parcial — "
                                                f"use o backup de segurança para reverter): {erro}"}

    db.reset_pool()  # força conexões novas depois da restauração
    return {"status": "ok", "message": "Banco PostgreSQL restaurado com sucesso."}
