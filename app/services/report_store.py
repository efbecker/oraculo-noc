"""
Armazenamento de relatórios (Opção A: metadados no banco, bytes em disco PRIVADO).

Por que este módulo existe
--------------------------
Antes, os relatórios eram gravados em ``app/static/`` e o link entregue no chat
apontava para ``/static/Dashboard_Executivo_<timestamp>.html``. No Flask, a pasta
``static`` é servida SEM autenticação, e o nome (um timestamp) é adivinhável —
ou seja, num ambiente multi-cliente, o relatório de um cliente podia ser baixado
por outro (ou por qualquer pessoa) só conhecendo/adivinhando a URL.

A correção (Opção A):
  * o HTML é gravado numa pasta PRIVADA (``instance/reports/``), fora de static;
  * o nome do arquivo é um token aleatório e inadivinhável;
  * o banco guarda apenas os METADADOS + o escopo (grupo/tenant) — nunca o HTML,
    para não inchar o banco com blobs de vários MB;
  * o download passa por uma rota AUTENTICADA que confere o tenant do usuário;
  * relatórios vencidos (retenção) são apagados automaticamente.

Nada aqui levanta exceção para fora: relatório é um recurso auxiliar; se o
armazenamento falhar, o chamador degrada com elegância em vez de quebrar o chat.
"""

import os
import time
import secrets
from datetime import datetime

from app import db

# Pasta PRIVADA de relatórios: instance/reports/ (fora de app/static).
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../app
_PROJECT_DIR = os.path.dirname(_APP_DIR)
INSTANCE_DIR = os.path.join(_PROJECT_DIR, 'instance')
REPORTS_DIR = os.path.join(INSTANCE_DIR, 'reports')
os.makedirs(REPORTS_DIR, exist_ok=True)

# Retenção padrão (dias). Passado o prazo, arquivo + registro são removidos na
# próxima geração de relatório (limpeza oportunista) ou no boot. Ajuste aqui.
RETENCAO_DIAS = 30


def _agora_iso():
    return datetime.now().strftime('%d/%m/%Y %H:%M')


def salvar_relatorio(html, *, grupo, criado_por, titulo=''):
    """Grava o HTML numa pasta privada e registra os metadados no banco.

    Retorna o ``id`` (token aleatório inadivinhável) usado na URL autenticada
    ``/reports/<id>``, ou ``None`` se algo falhar (o chamador então mostra uma
    mensagem sem botão de download, em vez de derrubar o chat)."""
    try:
        purgar_expirados()  # limpeza oportunista e barata
        rid = secrets.token_urlsafe(24)  # ~32 chars — inadivinhável
        arquivo = f"{rid}.html"
        caminho = os.path.join(REPORTS_DIR, arquivo)
        with open(caminho, 'w', encoding='utf-8') as f:
            f.write(html)
        tamanho = os.path.getsize(caminho)
        agora = time.time()
        db.execute(
            "INSERT INTO reports (id, grupo_cliente, criado_por, titulo, arquivo, "
            "tamanho, criado_em, criado_ts, expira_ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, grupo or '', criado_por or '', titulo or '', arquivo,
             int(tamanho), _agora_iso(), agora, agora + RETENCAO_DIAS * 86400)
        )
        return rid
    except Exception as e:
        print(f"[report_store] Falha ao salvar relatório: {e}")
        return None


def obter_relatorio(rid):
    """Metadados do relatório (dict) ou None. Acrescenta 'caminho' (path absoluto
    do arquivo no disco) para o servidor da rota autenticada."""
    if not rid:
        return None
    try:
        row = db.query_one("SELECT * FROM reports WHERE id=?", (rid,))
    except Exception as e:
        print(f"[report_store] Falha ao consultar relatório: {e}")
        return None
    if not row:
        return None
    row['caminho'] = os.path.join(REPORTS_DIR, row['arquivo']) if row.get('arquivo') else None
    return row


def usuario_pode_acessar(row, *, grupo_sessao, perfil):
    """Regra de tenant do download:
       - ADMIN/ADMINISTRADOR: acessa qualquer relatório;
       - conta com grupo 'GLOBAL' (master): idem;
       - demais: o grupo do relatório TEM de bater com o grupo do usuário.
    Sem correspondência = 403. É a barreira que faltava quando tudo vivia em
    /static sem autenticação."""
    if str(perfil or '').upper().strip() in ('ADMIN', 'ADMINISTRADOR'):
        return True
    g = str(grupo_sessao or '').strip()
    if g == 'GLOBAL':
        return True
    return bool(g) and row.get('grupo_cliente') == g


def listar_por_grupo(grupo, limite=50):
    """Histórico de relatórios de um grupo (mais recentes primeiro). Útil para
    uma futura tela 'Meus relatórios'. Nunca levanta exceção."""
    try:
        return db.query_all(
            "SELECT id, titulo, criado_por, criado_em, tamanho FROM reports "
            "WHERE grupo_cliente=? ORDER BY criado_ts DESC LIMIT ?",
            (grupo or '', int(limite))
        )
    except Exception:
        return []


def purgar_expirados():
    """Apaga arquivos e registros vencidos. Silencioso e tolerante a falha —
    nunca deve impedir a geração de um novo relatório."""
    try:
        agora = time.time()
        vencidos = db.query_all(
            "SELECT id, arquivo FROM reports WHERE expira_ts IS NOT NULL AND expira_ts < ?",
            (agora,)
        )
        for r in vencidos:
            try:
                if r.get('arquivo'):
                    p = os.path.join(REPORTS_DIR, r['arquivo'])
                    if os.path.isfile(p):
                        os.remove(p)
            except Exception:
                pass  # arquivo já sumiu: segue e remove o registro mesmo assim
            try:
                db.execute("DELETE FROM reports WHERE id=?", (r['id'],))
            except Exception:
                pass
    except Exception as e:
        print(f"[report_store] Falha ao purgar expirados: {e}")
