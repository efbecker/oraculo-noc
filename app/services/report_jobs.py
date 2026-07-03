"""
Execução assíncrona de relatórios (FinOps / gráficos do Zabbix) com barra de
progresso AO VIVO, reaproveitando o mesmo widget do scan Nmap.

Importante: NÃO altera o formato do relatório. A função geradora (do zabbix.py)
é chamada intacta; aqui apenas orquestramos o progresso e a entrega no chat.
"""
import threading
import sqlite3
import uuid
import logging
from datetime import datetime
from config import DB_PATH
from app.services.secops import update_scan_progress, init_scan_db

logger = logging.getLogger('Oraculo')

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


def start_report_job(username, label, generator_fn):
    """Inicia um relatório em background.
    - username: dono (para isolamento multi-tenant no /api/active_scans)
    - label: texto que aparece no widget (ex.: 'Relatório FinOps (30 dias)')
    - generator_fn: função SEM argumentos que retorna o HTML/markdown do relatório
    Retorna o job_id (str) ou None em caso de falha.
    """
    init_scan_db()  # garante a tabela 'scans' e a coluna 'owner'
    job_id = str(uuid.uuid4())
    try:
        with sqlite3.connect(DB_PATH, timeout=20) as conn:
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute(
                "INSERT INTO scans (id, target, progress, stage, status, owner) VALUES (?, ?, 0, ?, 'running', ?)",
                (job_id, label, FIRST_STAGE, username)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"[Report] Falha ao criar job para '{username}': {e}")
        return None

    def run():
        lock = threading.Lock()
        state = {"progress": 1.0, "stage": FIRST_STAGE}
        stop = threading.Event()

        def heartbeat():
            # Faz a barra avançar suavemente rumo ao teto, trocando o texto do
            # estágio conforme o progresso, para não parecer travada.
            while not stop.wait(1.5):
                with lock:
                    if state["progress"] < _CEILING - 0.5:
                        state["progress"] += max(0.5, (_CEILING - state["progress"]) * 0.05)
                        for limiar, texto in _STAGES:
                            if state["progress"] >= limiar:
                                state["stage"] = texto
                        p, s = round(state["progress"], 1), state["stage"]
                    else:
                        p = None
                if p is not None:
                    update_scan_progress(job_id, p, s)

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
                    "INSERT INTO chat_history (username, sender, content, is_html, timestamp) VALUES (?, 'bot', ?, ?, ?)",
                    (username, reply_content, is_html, now_str)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"[Report] Erro ao gravar relatório no chat: {e}")

        update_scan_progress(job_id, 100, "Concluído", final_status)

    threading.Thread(target=run, daemon=True).start()
    return job_id
