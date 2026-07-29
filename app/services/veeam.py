"""
INTEGRAÇÃO COM O VEEAM BACKUP ENTERPRISE MANAGER (VBEM) — Oráculo Backup
========================================================================
Dá ao Oráculo visão do BACKUP de cada cliente, do mesmo jeito que o módulo
zabbix.py dá visão da INFRAESTRUTURA. Segue os mesmos padrões já usados no
projeto (ai_providers.py / db.py):

    1) instance/veeam_config.json  (gravado pelo painel Admin > Veeam)
    2) Variáveis de ambiente:       VEEAM_URL, VEEAM_USER, VEEAM_PASSWORD, VEEAM_VERIFY_TLS
    3) Sem configuração: o chat avisa que é preciso configurar no painel
       (nada de stack trace na cara do cliente).

DECISÕES DE PROJETO (por que é assim)
-------------------------------------
• SOMENTE LEITURA. A API do Veeam também faz start/stop de job, restore e
  failover — NADA disso é exposto aqui. O módulo só faz GET. Backup é dado
  sensível: o cliente vê o SEU, e ninguém dispara ação pelo chat.

• MULTI-TENANT por PREFIXO DE NOME. No ambiente da Armazém todo job segue a
  convenção CLIENTE_...  (ex.: SUPREMA_BKP-7D, DOCOL-DC_ARMAZEM-..., 5-ESTRELAS_DIARIO).
  O "cliente" é tudo antes do PRIMEIRO '_'. Essa chave funciona nos dois
  backup servers (vCloud Director e vSphere/Hyper-V). Cada grupo_cliente do
  Oráculo é mapeado (Admin > Veeam) para um valor de prefixo (ou, se preferir,
  para o nome do tenant vCD). Grupo GLOBAL/admin enxerga tudo — igual ao Zabbix.

• AUTENTICAÇÃO. O VBEM NÃO usa Bearer nem JSON-RPC. O fluxo é:
    POST /sessionMngr/?v=latest  com HTTP Basic  ->  devolve o token no HEADER
    'X-RestSvcSessionId', que é reusado nas chamadas seguintes. Aqui a sessão é
    cacheada por alguns minutos (logar a cada mensagem pesa e polui o log de
    sessões do EM). Em 401, refaz o login uma vez.

• ESCALA. São centenas de jobs (o ambiente atual tem 600+). A coleta pesada é
  cacheada por CACHE_TTL segundos e COMPARTILHADA entre todos os clientes:
  uma única busca alimenta o recorte de cada grupo. Sem isso, o EM apanharia a
  cada mensagem de chat.
"""
import os
import json
import time
import threading
import requests

# O EM quase sempre usa certificado interno/self-signed. Quando o admin deixa a
# verificação TLS desligada, silenciamos o aviso barulhento do urllib3 (senão
# ele polui o journalctl a cada chamada).
try:
    from urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)  # type: ignore
except Exception:
    pass

from app.services import branding

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
VEEAM_CONFIG_FILE = os.path.join(INSTANCE_DIR, 'veeam_config.json')
VEEAM_MAP_FILE = os.path.join(INSTANCE_DIR, 'veeam_map.json')

TIMEOUT = 60          # segundos por chamada HTTP ao EM
SESSION_TTL = 480     # reusa a sessão logada por 8 min (o EM expira ~15 min)
CACHE_TTL = 60        # cache da coleta pesada de jobs (compartilhado entre clientes)
API_VERSION = 'latest'

_state_lock = threading.Lock()
_session_lock = threading.Lock()
_cache_lock = threading.Lock()

# Cache de sessão logada por base_url: {base_url: (token, expira_em)}
_sessions = {}
# Cache da coleta pesada (global, fatiado por cliente em memória):
#   {'ts': epoch, 'jobs': [ {..job..}, ... ]}
_jobs_cache = {'ts': 0.0, 'jobs': None}


# ==========================================================================
# CONFIGURAÇÃO GLOBAL (conexão com o EM)
# ==========================================================================
def _defaults_from_env():
    verify = (os.environ.get('VEEAM_VERIFY_TLS') or '').strip().lower()
    return {
        "url": (os.environ.get('VEEAM_URL') or '').strip().rstrip('/'),
        "user": (os.environ.get('VEEAM_USER') or '').strip(),
        "password": (os.environ.get('VEEAM_PASSWORD') or '').strip(),
        # Padrão: NÃO verificar (cert interno). Ligue com VEEAM_VERIFY_TLS=1.
        "verify_tls": verify in ('1', 'true', 'yes', 'on'),
    }


def _load_state():
    cfg = _defaults_from_env()
    try:
        if os.path.exists(VEEAM_CONFIG_FILE):
            with open(VEEAM_CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            cfg.update({k: v for k, v in saved.items() if v is not None})
    except Exception:
        pass
    return cfg


_STATE = _load_state()


def is_configured():
    with _state_lock:
        return bool(_STATE.get('url') and _STATE.get('user') and _STATE.get('password'))


def current_config(mask_password=True):
    """Config atual para exibir no painel. Senha SEMPRE mascarada por padrão —
    a senha real nunca volta para o navegador."""
    with _state_lock:
        cfg = dict(_STATE)
    if mask_password:
        p = cfg.get('password') or ''
        cfg['password'] = ('••••••••' if p else '')
    return cfg


def reconfigure(url, user, password='', verify_tls=False, persist=True):
    """Aplica a nova config em memória (vale já para as próximas mensagens) e,
    se persist, grava em instance/veeam_config.json (chmod 600 — tem senha!).
    Senha em branco = mantém a já salva (mesma regra da IA/banco)."""
    url = (url or '').strip().rstrip('/')
    with _state_lock:
        senha_atual = _STATE.get('password') or ''
        _STATE['url'] = url
        _STATE['user'] = (user or '').strip()
        _STATE['password'] = (password or '').strip() or senha_atual
        _STATE['verify_tls'] = bool(verify_tls)
        snapshot = dict(_STATE)
    # Config mudou: invalida sessões e cache antigos.
    _limpar_sessoes()
    _invalidar_cache()
    if persist:
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        with open(VEEAM_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, indent=2)
        try:
            os.chmod(VEEAM_CONFIG_FILE, 0o600)
        except Exception:
            pass


# ==========================================================================
# MAPA CLIENTE -> ESCOPO NO VEEAM  (multi-tenant)
# ==========================================================================
# instance/veeam_map.json:
#   { "SUPREMA": {"mode": "prefix", "value": "SUPREMA"},
#     "POLLUX":  {"mode": "tenant", "value": "POLLUX"} }
# A chave do dict é o grupo_cliente do Oráculo (users.grupo_cliente).
#   mode 'prefix'  -> casa jobs cujo nome começa por value + '_' (ou value)
#   mode 'tenant'  -> casa jobs cujo tenant/organização vCD == value
def _load_map():
    try:
        if os.path.exists(VEEAM_MAP_FILE):
            with open(VEEAM_MAP_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


_MAP = _load_map()


def reload_from_disk():
    """Recarrega config e mapeamento do Veeam a partir do disco, sem reiniciar
    o processo. Usado após restaurar um backup (instance/veeam_config.json e
    instance/veeam_map.json acabaram de ser sobrescritos)."""
    global _STATE, _MAP
    with _state_lock:
        _STATE = _load_state()
        _MAP = _load_map()


def _save_map(snapshot):
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    with open(VEEAM_MAP_FILE, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, indent=2)


def list_client_maps():
    with _state_lock:
        return {k: dict(v) for k, v in _MAP.items()}


def save_client_map(grupo, mode='prefix', value=''):
    grupo = (grupo or '').strip()
    if not grupo:
        raise ValueError("Informe o grupo do cliente (igual ao 'grupo_cliente' do usuário).")
    mode = (mode or 'prefix').strip().lower()
    if mode not in ('prefix', 'tenant'):
        raise ValueError("Modo inválido (use 'prefix' ou 'tenant').")
    value = (value or '').strip()
    if not value:
        raise ValueError("Informe o valor (prefixo do nome do job ou nome do tenant).")
    with _state_lock:
        _MAP[grupo] = {"mode": mode, "value": value}
        snapshot = {k: dict(v) for k, v in _MAP.items()}
    _save_map(snapshot)


def delete_client_map(grupo):
    with _state_lock:
        if grupo in _MAP:
            del _MAP[grupo]
            snapshot = {k: dict(v) for k, v in _MAP.items()}
        else:
            return False
    _save_map(snapshot)
    return True


def _scope_for_group(grupo):
    """Resolve o escopo do cliente. Retorna None quando o grupo enxerga TUDO
    (GLOBAL/admin) ou dict {mode, value}. Se não houver mapa explícito, cai no
    padrão sensato: prefixo = o próprio nome do grupo (a convenção do ambiente)."""
    g = (grupo or '').strip()
    if not g or g.upper() == 'GLOBAL':
        return None  # vê tudo
    with _state_lock:
        m = _MAP.get(g)
    if m:
        return dict(m)
    # Sem mapeamento explícito: assume que o prefixo do job é o próprio grupo.
    return {"mode": "prefix", "value": g}


# ==========================================================================
# AUTENTICAÇÃO / TRANSPORTE (X-RestSvcSessionId)
# ==========================================================================
def _limpar_sessoes():
    with _session_lock:
        _sessions.clear()


def _invalidar_cache():
    with _cache_lock:
        _jobs_cache['ts'] = 0.0
        _jobs_cache['jobs'] = None


def _cfg_ou_estado(cfg):
    if cfg:
        return cfg
    with _state_lock:
        return dict(_STATE)


def _logon(cfg):
    """Faz logon no EM e devolve o token de sessão (X-RestSvcSessionId)."""
    base = (cfg.get('url') or '').rstrip('/')
    if not base:
        raise ValueError("URL do Veeam não configurada.")
    r = requests.post(
        f"{base}/sessionMngr/?v={API_VERSION}",
        auth=(cfg.get('user') or '', cfg.get('password') or ''),
        headers={"Accept": "application/json"},
        verify=bool(cfg.get('verify_tls')),
        timeout=TIMEOUT,
    )
    if r.status_code in (401, 403):
        raise PermissionError("Usuário ou senha do Veeam inválidos (HTTP %s)." % r.status_code)
    r.raise_for_status()
    token = r.headers.get('X-RestSvcSessionId')
    if not token:
        raise RuntimeError("O Veeam autenticou mas não devolveu o token de sessão (X-RestSvcSessionId).")
    with _session_lock:
        _sessions[base] = (token, time.time() + SESSION_TTL)
    return token


def _token_valido(cfg):
    base = (cfg.get('url') or '').rstrip('/')
    with _session_lock:
        tok = _sessions.get(base)
    if tok and tok[1] > time.time():
        return tok[0]
    return _logon(cfg)


def veeam_get(path, cfg=None):
    """GET autenticado no EM. Reusa a sessão; em 401, refaz login uma vez.
    'path' começa com '/' e é relativo à base (ex.: '/jobs?format=Entities')."""
    cfg = _cfg_ou_estado(cfg)
    base = (cfg.get('url') or '').rstrip('/')
    token = _token_valido(cfg)

    def _do(tok):
        return requests.get(
            f"{base}{path}",
            headers={"X-RestSvcSessionId": tok, "Accept": "application/json"},
            verify=bool(cfg.get('verify_tls')),
            timeout=TIMEOUT,
        )

    r = _do(token)
    if r.status_code == 401:
        _limpar_sessoes()
        token = _logon(cfg)
        r = _do(token)
    r.raise_for_status()
    # Algumas rotas podem devolver vazio; protege o .json().
    try:
        return r.json()
    except ValueError:
        return {}


def test_connection(url, user, password='', verify_tls=False):
    """Usado pelo botão 'Testar Conexão' do painel. Loga e bate em
    /reports/summary/overview — sem salvar nada. Retorna (ok, mensagem)."""
    url = (url or '').strip().rstrip('/')
    if not url:
        return False, "Informe a URL do Veeam Enterprise Manager (ex.: https://host:9398/api)."
    # Senha em branco no teste = usar a já salva (o painel nunca ecoa a senha).
    if not (password or '').strip():
        with _state_lock:
            password = _STATE.get('password') or ''
    cfg = {"url": url, "user": (user or '').strip(),
           "password": (password or '').strip(), "verify_tls": bool(verify_tls)}
    try:
        token = _logon(cfg)  # já valida usuário/senha e a URL
        try:
            ov = veeam_get('/reports/summary/overview', cfg) or {}
        except Exception:
            ov = {}
        jobs = _num(ov.get('RunningJobs')) + _num(ov.get('ScheduledJobs'))
        extra = f" · {jobs} job(s) visíveis" if jobs else ""
        return True, f"✅ Conexão OK com o Veeam em {url}{extra}."
    except PermissionError as e:
        return False, str(e)
    except requests.exceptions.SSLError:
        return False, ("Falha de certificado TLS. Se o EM usa certificado interno/self-signed, "
                       "desmarque 'Verificar certificado TLS' e teste de novo.")
    except requests.exceptions.ConnectTimeout:
        return False, f"Tempo esgotado ao conectar em {url} (firewall/porta 9398?)."
    except requests.exceptions.RequestException as e:
        return False, f"Falha de conexão: {e}"
    except Exception as e:
        return False, f"Falha: {e}"


# ==========================================================================
# COLETA E NORMALIZAÇÃO DE JOBS
# ==========================================================================
def _num(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


def _como_lista(v):
    """JSON convertido de XML às vezes vira dict único quando há 1 item, e lista
    quando há vários. Normaliza sempre para lista."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _cavar_jobs(payload):
    """Extrai a lista de jobs de /jobs?format=Entities, tolerando as variações
    de aninhamento do JSON derivado do XML do EM."""
    if not isinstance(payload, dict):
        return []
    # Caminho usual: {"Entities": {"Jobs": {"Job": [...]}}}
    ent = payload.get('Entities') or {}
    jobs = (((ent.get('Jobs') or {}).get('Job')) if isinstance(ent, dict) else None)
    if jobs is not None:
        return _como_lista(jobs)
    # Fallbacks defensivos.
    if 'Jobs' in payload and isinstance(payload['Jobs'], dict):
        return _como_lista(payload['Jobs'].get('Job'))
    if 'Job' in payload:
        return _como_lista(payload['Job'])
    return []


def _prefixo_cliente(job_name):
    """Cliente = tudo antes do PRIMEIRO '_'. Ex.: 'DOCOL-DC_ARMAZEM-...' -> 'DOCOL-DC'."""
    if not job_name:
        return ''
    return job_name.split('_', 1)[0].strip()


def _normalizar_job(j):
    nome = j.get('Name') or j.get('name') or ''
    return {
        "name": nome,
        "client": _prefixo_cliente(nome),
        "type": j.get('JobType') or j.get('Platform') or '',
        "platform": j.get('Platform') or '',
        "last_result": (j.get('LastResult') or 'None'),      # Success/Warning/Failed/None
        "last_run": j.get('LastRun') or '',
        "next_run": j.get('NextRun') or '',
        "schedule_enabled": j.get('ScheduleEnabled'),
        "description": j.get('Description') or '',
        # tenant/organização (quando vCloud Director expõe) — usado no modo 'tenant'
        "tenant": (j.get('TenantName') or j.get('OrganizationName') or ''),
    }


def _coletar_jobs(cfg=None, force=False):
    """Coleta TODOS os jobs do EM (uma vez), normaliza e CACHEIA por CACHE_TTL.
    O mesmo dataset alimenta o recorte de todos os clientes."""
    agora = time.time()
    with _cache_lock:
        if (not force) and _jobs_cache['jobs'] is not None and (agora - _jobs_cache['ts'] < CACHE_TTL):
            return _jobs_cache['jobs']
    payload = veeam_get('/jobs?format=Entities', cfg)
    jobs = [_normalizar_job(j) for j in _cavar_jobs(payload)]
    with _cache_lock:
        _jobs_cache['ts'] = time.time()
        _jobs_cache['jobs'] = jobs
    return jobs


def _casa_cliente(job, scope):
    """True se o job pertence ao escopo do cliente (ou scope None = tudo)."""
    if scope is None:
        return True
    valor = (scope.get('value') or '').strip().lower()
    if not valor:
        return False
    if scope.get('mode') == 'tenant':
        return (job.get('tenant') or '').strip().lower() == valor
    # modo prefixo (padrão): compara o "cliente" derivado do nome do job
    return (job.get('client') or '').strip().lower() == valor


def jobs_do_cliente(grupo, cfg=None):
    """Lista de jobs (normalizados) que o grupo_cliente pode ver."""
    scope = _scope_for_group(grupo)
    return [j for j in _coletar_jobs(cfg) if _casa_cliente(j, scope)]


# ==========================================================================
# AGREGAÇÃO / RESUMOS
# ==========================================================================
_RESULT_LABEL = {
    'success': 'Sucesso', 'warning': 'Atenção', 'failed': 'Falha',
    'none': 'Sem execução', 'running': 'Em execução',
}


def _resumir(jobs):
    """Conta jobs por resultado da última execução."""
    cont = {'Success': 0, 'Warning': 0, 'Failed': 0, 'None': 0}
    for j in jobs:
        r = (j.get('last_result') or 'None')
        cont[r] = cont.get(r, 0) + 1
    return cont


def status_backup_texto(grupo, cfg=None):
    """Resumo em Markdown para resposta INLINE rápida no chat."""
    if not is_configured():
        return ("💾 **Veeam não configurado.**\n\nPeça ao administrador para configurar a conexão "
                "em **Admin → Veeam**.")
    try:
        jobs = jobs_do_cliente(grupo, cfg)
    except PermissionError as e:
        return f"🚨 **Falha de autenticação no Veeam.** {e}"
    except Exception as e:
        return f"🚨 **Não consegui consultar o Veeam.**\n`{e}`"

    if not jobs:
        return (f"💾 Não encontrei jobs de backup no escopo do seu ambiente "
                f"({grupo or 'GLOBAL'}). Confira o mapeamento em Admin → Veeam.")

    c = _resumir(jobs)
    total = len(jobs)
    linhas = [f"### 💾 Backup — {grupo or 'GLOBAL'} ({total} job(s))", ""]
    linhas.append(f"- ✅ Sucesso: **{c.get('Success', 0)}**")
    if c.get('Warning', 0):
        linhas.append(f"- ⚠️ Atenção: **{c.get('Warning', 0)}**")
    if c.get('Failed', 0):
        linhas.append(f"- ❌ Falha: **{c.get('Failed', 0)}**")
    if c.get('None', 0):
        linhas.append(f"- ⏸️ Sem execução: **{c.get('None', 0)}**")

    # Destaque para o que está com problema (o que o cliente mais quer saber).
    problemas = [j for j in jobs if (j.get('last_result') in ('Warning', 'Failed'))]
    if problemas:
        linhas.append("\n**Jobs que pedem atenção:**")
        for j in problemas[:15]:
            emoji = '❌' if j['last_result'] == 'Failed' else '⚠️'
            linhas.append(f"- {emoji} `{j['name']}` — última execução: {j['last_run'] or '—'}")
    else:
        linhas.append("\n🎉 Todos os backups do seu ambiente estão saudáveis.")
    return "\n".join(linhas)


def resumo_backup_para_ia(grupo, cfg=None):
    """Bloco curto de texto injetado no CONTEXTO da IA (obter_contexto_infra),
    para perguntas soltas tipo 'meu ambiente está seguro?'. Silencioso quando o
    Veeam não está configurado (não atrapalha quem só usa Zabbix)."""
    if not is_configured():
        return ""
    try:
        jobs = jobs_do_cliente(grupo, cfg)
    except Exception:
        return ""
    if not jobs:
        return ""
    c = _resumir(jobs)
    problemas = [j['name'] for j in jobs if j.get('last_result') in ('Warning', 'Failed')]
    txt = (f"\n--- ESTADO DO BACKUP (Veeam) — {grupo or 'GLOBAL'} ---\n"
           f"Total de jobs: {len(jobs)} | Sucesso: {c.get('Success',0)} | "
           f"Atenção: {c.get('Warning',0)} | Falha: {c.get('Failed',0)} | "
           f"Sem execução: {c.get('None',0)}\n")
    if problemas:
        txt += "Jobs com problema: " + ", ".join(problemas[:20]) + "\n"
    return txt


# ==========================================================================
# RELATÓRIO HTML (mesmo tema escuro do FinOps/SecOps)
# ==========================================================================
_BADGE = {
    'Success': ('#00e676', 'rgba(0,230,118,0.15)', '✅ Sucesso'),
    'Warning': ('#ff9e00', 'rgba(255,158,0,0.15)', '⚠️ Atenção'),
    'Failed':  ('#ff5252', 'rgba(255,82,82,0.15)', '❌ Falha'),
    'None':    ('#888',    'rgba(136,136,136,0.15)', '⏸️ Sem execução'),
}


def _card(valor, rotulo, cor):
    return (f'<div style="flex:1; min-width:130px; background:#1c1c1c; border:1px solid #2a2a2a;'
            f'border-radius:8px; padding:16px; text-align:center;">'
            f'<div style="font-size:30px; font-weight:700; color:{cor};">{valor}</div>'
            f'<div style="font-size:12px; color:#aaa; text-transform:uppercase; letter-spacing:.5px;">{rotulo}</div>'
            f'</div>')


def gerar_relatorio_backup_html(grupo, cfg=None):
    """Dashboard de backup HTML autocontido (para o fluxo de report_job)."""
    if not is_configured():
        return ("💾 **Veeam não configurado.** Peça ao administrador para configurar "
                "a conexão em **Admin → Veeam**.")
    try:
        jobs = jobs_do_cliente(grupo, cfg)
    except PermissionError as e:
        return f"🚨 **Falha de autenticação no Veeam.** {e}"
    except Exception as e:
        return f"⚠️ **Erro ao consultar o Veeam.**\n`{e}`"

    if not jobs:
        return (f"💾 Nenhum job de backup encontrado no escopo **{grupo or 'GLOBAL'}**. "
                f"Confira o mapeamento em Admin → Veeam.")

    jobs = sorted(jobs, key=lambda j: (j.get('last_result') != 'Failed',
                                       j.get('last_result') != 'Warning',
                                       j.get('name', '').lower()))
    c = _resumir(jobs)
    from datetime import datetime as _dt
    agora = _dt.now().strftime('%d/%m/%Y %H:%M')

    linhas = []
    for j in jobs:
        cor, bg, rotulo = _BADGE.get(j.get('last_result') or 'None', _BADGE['None'])
        linhas.append(
            f"<tr>"
            f"<td><strong>{j['name']}</strong>"
            f"<div style='font-size:11px;color:#777;'>{j.get('description','')[:70]}</div></td>"
            f"<td>{j.get('platform','') or j.get('type','')}</td>"
            f"<td><span style='background:{bg};color:{cor};padding:3px 10px;border-radius:10px;"
            f"font-size:12px;font-weight:bold;'>{rotulo}</span></td>"
            f"<td>{j.get('last_run','') or '—'}</td>"
            f"<td>{j.get('next_run','') or '—'}</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html><html lang='pt-br'><head><meta charset='UTF-8'>
<title>Backup Veeam | {grupo or 'GLOBAL'}</title><style>
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#121212; color:#e0e0e0; margin:0; padding:20px; }}
.container {{ max-width:960px; margin:auto; background:#161616; padding:36px; border-radius:12px; border:1px solid #2a2a2a; box-shadow:0 10px 40px rgba(0,0,0,.8); }}
h1 {{ text-align:center; color:#00e5ff; margin:0 0 6px; font-size:28px; letter-spacing:.5px; }}
.info {{ text-align:center; color:#888; font-size:13px; margin-bottom:26px; padding-bottom:14px; border-bottom:1px solid #333; }}
.cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:26px; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ border-bottom:1px solid #2a2a2a; padding:11px 12px; text-align:left; font-size:13.5px; }}
th {{ background:#242424; color:#00e5ff; text-transform:uppercase; font-size:11px; letter-spacing:.5px; }}
tr:hover td {{ background:#1c1c1c; }}
.footer {{ text-align:center; font-size:.8em; color:#666; margin-top:28px; }}
</style></head><body><div class='container'>
{branding.get_logo_html()}
<h1>💾 Oráculo Backup — Dashboard Veeam</h1>
<div class='info'><b>Ambiente:</b> {grupo or 'GLOBAL'} &nbsp;·&nbsp; <b>Data/Hora:</b> {agora} &nbsp;·&nbsp; <b>Total de jobs:</b> {len(jobs)}</div>
<div class='cards'>
{_card(c.get('Success',0), 'Sucesso', '#00e676')}
{_card(c.get('Warning',0), 'Atenção', '#ff9e00')}
{_card(c.get('Failed',0), 'Falha', '#ff5252')}
{_card(c.get('None',0), 'Sem execução', '#888')}
</div>
<table><thead><tr><th>Job</th><th>Plataforma</th><th>Última Execução</th><th>Quando</th><th>Próxima</th></tr></thead>
<tbody>{''.join(linhas)}</tbody></table>
<p class='footer'>Gerado por Oráculo Armazem Cloud · dados do Veeam Backup Enterprise Manager (somente leitura)</p>
</div></body></html>"""
    return html
