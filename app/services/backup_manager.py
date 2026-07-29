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
import io
import tarfile
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


# ==========================================================================
# CONFIGURAÇÕES FORA DO BANCO (viajam junto no backup)
# --------------------------------------------------------------------------
# Boa parte da configuração do produto NÃO fica no banco, e sim em arquivos
# JSON em instance/: a IA (global + perfis por cliente), o Veeam (conexão +
# mapeamento) e o logo do Branding. Antes, o backup só levava o banco — por
# isso uma instalação nova restaurava usuários e histórico, mas "perdia" a
# configuração de IA/Veeam/logo. Agora o backup é um PACOTE (.tar.gz) que
# inclui o dump do banco E essas configs.
#
# NÃO incluímos backup_config.json (agendamento) de propósito: o horário de
# backup é característica de cada servidor, e restaurá-lo faria uma máquina
# nova adotar o agendamento da origem sem querer.
def _itens_de_config():
    """(caminho_absoluto, nome_dentro_do_pacote) de cada config existente."""
    itens = []
    for nome in ('ai_config.json', 'ai_profiles.json',
                 'veeam_config.json', 'veeam_map.json'):
        p = os.path.join(db.INSTANCE_DIR, nome)
        if os.path.isfile(p):
            itens.append((p, 'config/' + nome))
    branding_dir = os.path.join(db.INSTANCE_DIR, 'branding')
    if os.path.isdir(branding_dir):
        for raiz, _dirs, arquivos in os.walk(branding_dir):
            for a in arquivos:
                full = os.path.join(raiz, a)
                rel = os.path.relpath(full, db.INSTANCE_DIR).replace(os.sep, '/')
                itens.append((full, 'config/' + rel))
    return itens


def _montar_pacote(db_dump_path, db_arcname, engine, dest):
    """Empacota (tar.gz) o dump do banco + as configs num único arquivo, com
    um manifest para o restore saber o motor de origem. chmod 600 porque o
    pacote contém segredos (chaves de API, senha do Veeam)."""
    manifest = {
        "format": 2,
        "app": "oraculo",
        "engine": engine,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode('utf-8')
    with tarfile.open(dest, "w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(manifest_bytes))
        tar.add(db_dump_path, arcname="database/" + db_arcname)
        for full, arc in _itens_de_config():
            tar.add(full, arcname=arc)
    try:
        os.chmod(dest, 0o600)
    except Exception:
        pass


def _ler_manifest_pacote(path):
    """Se 'path' for um pacote de backup (tar com manifest.json), devolve o
    manifest (dict). Caso contrário devolve None — é um backup 'cru' antigo
    (.db / .sql.gz), tratado pelo caminho de compatibilidade."""
    try:
        if not tarfile.is_tarfile(path):
            return None
        with tarfile.open(path, "r:*") as tar:
            try:
                membro = tar.getmember("manifest.json")
            except KeyError:
                return None
            dados = tar.extractfile(membro).read()
        return json.loads(dados.decode('utf-8'))
    except Exception:
        return None


def run_backup_now():
    """Executa um backup imediatamente (chamado pelo agendador OU pelo botão
    'Rodar agora' do painel). Retorna dict com o resultado."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    started_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    # Microssegundos + sufixo aleatório: garante que o nome do arquivo NUNCA
    # colide, mesmo quando dois backups acontecem no mesmo segundo (ex.: o
    # backup de segurança automático disparado logo antes de uma restauração).
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
    engine = "postgres" if db.is_postgres() else "sqlite"

    # Área temporária: primeiro geramos o dump do banco, depois empacotamos
    # (dump + configs) num único .tar.gz.
    tmpdir = os.path.join(BACKUP_DIR, f"_pkg_tmp_{uuid.uuid4().hex}")
    os.makedirs(tmpdir, exist_ok=True)

    try:
        if engine == "sqlite":
            db_dump = os.path.join(tmpdir, "oraculo.db")
            db_arcname = "oraculo.db"
            # Usa a API de backup do sqlite3 (segura com o banco em uso — não é
            # uma simples cópia de arquivo, que arriscaria pegar o .db no meio
            # de uma escrita).
            src_conn = sqlite3.connect(db.DB_PATH)
            dst_conn = sqlite3.connect(db_dump)
            with dst_conn:
                src_conn.backup(dst_conn)
            src_conn.close()
            dst_conn.close()

        else:
            cfg = db._pg_conn_kwargs()
            db_dump = os.path.join(tmpdir, "oraculo.sql.gz")
            db_arcname = "oraculo.sql.gz"
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
            with open(db_dump, "wb") as fdest:
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
                # Erro clássico: o pg_dump da máquina é mais ANTIGO que o servidor
                # (ex.: cliente 15 contra servidor 17). A mensagem crua do Postgres
                # é técnica demais; aqui traduzimos com a solução.
                if "server version" in erro.lower() or "aborting" in erro.lower():
                    erro += ("\n\n➡️ Versão do pg_dump incompatível com o servidor. Instale um cliente "
                             "igual ou mais novo que o servidor: apt install postgresql-client-17 "
                             "(ajuste o número para a versão do seu servidor).")
                _registrar(started_at, engine, "error", 0, None, f"pg_dump falhou: {erro}")
                return {"status": "error", "message": f"pg_dump falhou: {erro}"}

        # Empacota banco + configurações (IA, Veeam, branding) num só .tar.gz.
        dest = os.path.join(BACKUP_DIR, f"oraculo_backup_{stamp}.tar.gz")
        _montar_pacote(db_dump, db_arcname, engine, dest)
        size = os.path.getsize(dest)
        _registrar(started_at, engine, "ok", size, dest,
                   f"Backup {engine.upper()} + configurações (pacote) concluído.")
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
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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

    # Novo formato: PACOTE .tar.gz (banco + configs). O manifest diz o motor de
    # origem. Backups antigos (arquivo .db / .sql.gz cru) => manifest None e
    # seguem pelo caminho de compatibilidade (só banco).
    manifest = _ler_manifest_pacote(path)
    engine_backup = (manifest or {}).get('engine') or backup_engine_hint

    engine_atual = "postgres" if db.is_postgres() else "sqlite"
    if engine_backup and engine_backup != engine_atual:
        return {
            "status": "error",
            "message": f"Este backup foi gerado em {engine_backup.upper()}, mas o sistema está "
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
        if manifest:
            resultado = _restore_pacote(staging_path, engine_atual)
        elif engine_atual == "sqlite":
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


def _extrair_seguro(tar, destino):
    """Extrai o tar validando que nenhum membro escapa da pasta de destino
    (proteção contra path traversal em pacotes maliciosos/corrompidos, já que
    a restauração pode vir de um upload)."""
    base = os.path.abspath(destino)
    for membro in tar.getmembers():
        alvo = os.path.abspath(os.path.join(destino, membro.name))
        if alvo != base and not alvo.startswith(base + os.sep):
            raise ValueError(f"Entrada suspeita no pacote de backup: {membro.name}")
    tar.extractall(destino)


def _restaurar_configs(cfg_dir):
    """Copia as configs do pacote (config/*) de volta para instance/,
    preservando subpastas (ex.: branding/). JSONs voltam com chmod 600 (têm
    segredos). Devolve a lista de itens restaurados."""
    restauradas = []
    if not os.path.isdir(cfg_dir):
        return restauradas
    for raiz, _dirs, arquivos in os.walk(cfg_dir):
        for a in arquivos:
            full = os.path.join(raiz, a)
            rel = os.path.relpath(full, cfg_dir)  # ex.: ai_config.json | branding/logo.png
            destino = os.path.join(db.INSTANCE_DIR, rel)
            os.makedirs(os.path.dirname(destino) or db.INSTANCE_DIR, exist_ok=True)
            shutil.copyfile(full, destino)
            if destino.endswith('.json'):
                try:
                    os.chmod(destino, 0o600)
                except Exception:
                    pass
            restauradas.append(rel.replace(os.sep, '/'))
    return restauradas


def _recarregar_config_em_memoria():
    """Faz a IA e o Veeam relerem seus arquivos de config recém-restaurados,
    para valerem sem precisar reiniciar o processo."""
    try:
        from app.services import ai_providers
        ai_providers.reload_from_disk()
    except Exception as e:
        logger.warning(f"[Restore] Falha ao recarregar config de IA: {e}")
    try:
        from app.services import veeam
        veeam.reload_from_disk()
    except Exception as e:
        logger.warning(f"[Restore] Falha ao recarregar config do Veeam: {e}")


def _restore_pacote(bundle_path, engine_atual):
    """Restaura um pacote .tar.gz: extrai, restaura o banco (database/*) e
    devolve as configs (config/*) para instance/, recarregando-as em memória."""
    tmp = os.path.join(BACKUP_DIR, f"_extract_{uuid.uuid4().hex}")
    os.makedirs(tmp, exist_ok=True)
    try:
        with tarfile.open(bundle_path, "r:*") as tar:
            _extrair_seguro(tar, tmp)

        db_dir = os.path.join(tmp, "database")
        arquivos_db = sorted(os.listdir(db_dir)) if os.path.isdir(db_dir) else []
        if not arquivos_db:
            return {"status": "error", "message": "Pacote de backup inválido: falta a pasta 'database'."}
        db_dump = os.path.join(db_dir, arquivos_db[0])

        # 1) Banco
        if engine_atual == "sqlite":
            res = _restore_sqlite(db_dump)
        else:
            res = _restore_postgres(db_dump)
        if res.get("status") != "ok":
            return res

        # 2) Configurações (IA, Veeam, branding) + reload em memória
        restauradas = _restaurar_configs(os.path.join(tmp, "config"))
        _recarregar_config_em_memoria()

        extra = ""
        if restauradas:
            # resumo amigável (sem listar cada arquivo de branding)
            nomes = []
            if any(x.startswith('ai_') for x in restauradas): nomes.append("IA")
            if any(x.startswith('veeam') for x in restauradas): nomes.append("Veeam")
            if any(x.startswith('branding/') for x in restauradas): nomes.append("Branding/logo")
            extra = " Configurações restauradas: " + (", ".join(nomes) or "nenhuma") + "."
        return {"status": "ok", "message": res.get("message", "Banco restaurado.") + extra}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
