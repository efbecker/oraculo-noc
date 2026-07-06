"""
Execução assíncrona de relatórios (FinOps / gráficos do Zabbix) com barra de
progresso AO VIVO, reaproveitando o mesmo widget do scan Nmap.

Importante: NÃO altera o formato do relatório. A função geradora (do zabbix.py)
é chamada intacta; aqui apenas orquestramos o progresso e a entrega no chat.
"""
import threading
import sqlite3
import uuid
import time
import logging
from datetime import datetime
from config import DB_PATH
from app.services.secops import update_scan_progress, init_scan_db

logger = logging.getLogger('Oraculo')

# Tempo mínimo (segundos) que a barra ao vivo fica visível e progredindo, mesmo
# quando a geração é quase instantânea (ambientes pequenos). Sem isso, o widget
# aparecia e sumia antes do usuário ver a barra andar.
MIN_VISIBLE_SECONDS = 6.0

# Estágios exibidos no widget (limiar de progresso -> texto). Como o Zabbix não
# nos dá progresso granular, avançamos por tempo (heartbeat) até um teto e
# saltamos para 100% quando a geração termina.
_STAGES = [
    (0,  "Conectando ao Zabbix..."),
    (15, "Coletando métricas dos hosts..."),
    (55, "Processando séries temporais..."),
    (85, "Montando dashboard..."),
]
_CEILING = 92.0
FIRST_STAGE = _STAGES[0][1]


def _remover_barra_duplicada(html):
    """Remove a barra de progresso 'falsa' que o relatório FinOps embute na mensagem
    de chat. Agora que o widget AO VIVO mostra o progresso, essa barra estática em
    100% ficou redundante (era a 'versão anterior' que aparecia no fim).

    Atua APENAS na mensagem de chat do relatório — o dashboard baixável (escrito em
    disco no zabbix.py) NÃO é tocado. Se o marcador não existir, devolve como veio
    (fail-safe: nunca corta um relatório que não seja o esperado).
    """
    if not isinstance(html, str):
        return html
    # O bloco da barra é o primeiro trecho e termina no primeiro </style>;
    # tudo depois é o '✅ Relatório Concluído' + botão de download.
    if "Processamento FinOps Concluído" in html and "</style>" in html:
        return html.split("</style>", 1)[1]
    return html


def start_report_job(username, label, generator_fn, session_id=None):
    """Inicia um relatório em background.
    - username: dono (para isolamento multi-tenant no /api/active_scans)
    - label: texto que aparece no widget (ex.: 'Relatório FinOps (30 dias)')
    - generator_fn: função SEM argumentos que retorna o HTML/markdown do relatório
    - session_id: sessão de chat onde o relatório final deve aparecer
    Retorna o job_id (str) ou None em caso de falha.
    """
    init_scan_db()  # garante a tabela 'scans' e as colunas 'owner'/'session_id'
    job_id = str(uuid.uuid4())
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as conn:
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute(
                "INSERT INTO scans (id, target, progress, stage, status, owner, session_id) VALUES (?, ?, 0, ?, 'running', ?, ?)",
                (job_id, label, FIRST_STAGE, username, session_id)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"[Report] Falha ao criar job para '{username}': {e}")
        return None

    def run():
        inicio = time.time()
        stop = threading.Event()

        def heartbeat():
            # Progresso baseado em TEMPO: enche suave de ~3% até o teto (92%) ao
            # longo de MIN_VISIBLE_SECONDS e depois segura no teto até a geração
            # terminar. Assim a barra sempre aparece progredindo, igual ao scan.
            while not stop.wait(0.6):
                frac = min(1.0, (time.time() - inicio) / MIN_VISIBLE_SECONDS)
                p = 3.0 + frac * (_CEILING - 3.0)
                stage = FIRST_STAGE
                for limiar, texto in _STAGES:
                    if p >= limiar:
                        stage = texto
                update_scan_progress(job_id, round(p, 1), stage)

        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()

        reply_content = ""
        final_status = "finished"
        try:
            reply_content = _remover_barra_duplicada(generator_fn() or "Relatório vazio.")
        except Exception as e:
            logger.error(f"[Report] Erro ao gerar relatório para '{username}': {e}")
            reply_content = f"⚠️ **Erro ao gerar o relatório.**\n`{e}`"
            final_status = "error"
        finally:
            # Segura a barra visível/progredindo por um tempo mínimo mesmo quando a
            # geração foi muito rápida, para o usuário efetivamente ver o progresso.
            restante = MIN_VISIBLE_SECONDS - (time.time() - inicio)
            if restante > 0:
                time.sleep(restante)
            stop.set()
            hb.join(timeout=4)

        # Entrega no chat (mesma ordem do scan: grava a mensagem ANTES de marcar
        # o job como concluído, para evitar corrida na hora do frontend recarregar).
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
        # Sucesso => o relatório é HTML (is_html=1) e mantém o formato original.
        is_html = 1 if final_status == "finished" else 0
        try:
            with sqlite3.connect(DB_PATH, timeout=20) as conn:
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute(
                    "INSERT INTO chat_history (username, sender, content, is_html, timestamp, session_id) VALUES (?, 'bot', ?, ?, ?, ?)",
                    (username, reply_content, is_html, now_str, session_id)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"[Report] Erro ao gravar relatório no chat: {e}")

        update_scan_progress(job_id, 100, "Concluído", final_status)

    threading.Thread(target=run, daemon=True).start()
    return job_id
