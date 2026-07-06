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


def _registrar(started_at, engine, status, size_bytes, file_path, message):
    try:
        db.execute(
            "INSERT INTO backups (started_at, engine, status, size_bytes, file_path, message) VALUES (?,?,?,?,?,?)",
            (started_at, engine, status, size_bytes, file_path, message)
        )
    except Exception as e:
        logger.error(f"[Backup] Falha ao registrar histórico: {e}")


def _aplicar_retencao(retention):
    """Mantém só os N backups mais recentes em disco (apaga o arquivo mais
    antigo além do limite). O histórico no banco NÃO é apagado — só o arquivo."""
    try:
        rows = db.query_all("SELECT id, file_path FROM backups WHERE status='ok' ORDER BY id DESC")
        for row in rows[retention:]:
            fp = row.get("file_path")
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
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
                "-U", cfg["user"], "-d", cfg["dbname"], "--no-password"
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
    return db.query_all("SELECT id, started_at, engine, status, size_bytes, file_path, message FROM backups ORDER BY id DESC LIMIT ?", (limit,))


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
