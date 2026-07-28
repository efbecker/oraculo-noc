import os
import re
import math
import time
import requests
import io
import base64
from datetime import datetime
from app.settings import ZABBIX_URL
from app.services import branding

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as ticker
from matplotlib.figure import Figure
import matplotlib.gridspec as gridspec

plt.rcParams['figure.facecolor'] = '#121212'
plt.rcParams['axes.facecolor'] = '#1e1e1e'
plt.rcParams['axes.edgecolor'] = '#333333'
plt.rcParams['text.color'] = '#e0e0e0'
plt.rcParams['xtick.color'] = '#888888'
plt.rcParams['ytick.color'] = '#888888'
plt.rcParams['font.family'] = 'sans-serif'

# Antes: lista de permissão (allowlist) curta demais — qualquer ponto de montagem
# fora dela (ex.: /boot, /srv, D:\, /mnt/dados) era descartado mesmo se detectado.
# Agora: lista de BLOQUEIO de pseudo-sistemas de arquivo (não são "disco real").
# Qualquer outro ponto de montagem/drive é aceito.
DISCOS_BLOQUEADOS_REGEX = re.compile(
    r'(?:^/(?:dev|proc|sys|run|snap|boot/efi)(?:/|$))|'
    r'\b(tmpfs|devtmpfs|overlay|overlay2|squashfs|aufs|cgroup|cgroup2|autofs|iso9660|docker)\b',
    re.IGNORECASE
)

# Extrai o parâmetro de ponto de montagem/drive e o tipo (pused/pfree) direto da
# CHAVE técnica do item Zabbix (ex.: vfs.fs.size[/,pused] ou vfs.fs.dependent[C:,pfree]).
# É a forma robusta de detectar disco: a chave é igual em qualquer idioma/template
# oficial do Zabbix, ao contrário do nome do item (que varia muito por tradução).
DISK_KEY_REGEX = re.compile(r'vfs\.fs\.(?:size|dependent)\[\s*([^,\]]+?)\s*(?:,\s*(pused|pfree|used|free|total))?\s*\]', re.IGNORECASE)

# Chave de disco do template VMware (Monitoração simples): o mount é o 3º
# parâmetro e o modo o 4º — ex.: vmware.vm.vfs.fs.size[{$VMWARE.URL},{$VMWARE.VM.UUID},C:\,pfree].
# Sem este padrão, TODO ambiente VMware (vCenter/vSphere) ficava invisível
# para a detecção por chave (o relatório FinOps só achava discos pelo
# fallback de NOME, e a previsão de capacidade não achava nada).
VMWARE_DISK_KEY_REGEX = re.compile(
    r'vmware\.vm\.vfs\.fs\.size\[\s*[^,\]]*\s*,\s*[^,\]]*\s*,\s*([^,\]]+?)\s*(?:,\s*(pused|pfree|used|free|total|usage))?\s*\]',
    re.IGNORECASE
)

def _parse_disk_key(key_):
    """Retorna (mount_point, kind) a partir da key_ do item, ou (None, None) se a
    key não for reconhecida como item de espaço em disco do Zabbix.
    Suporta o padrão clássico (vfs.fs.size/dependent) e o padrão VMware
    (vmware.vm.vfs.fs.size) — este último testado PRIMEIRO, pois a chave
    VMware contém 'vfs.fs.size' dentro dela e o regex genérico capturaria
    a URL do vCenter como se fosse o mount."""
    if not key_:
        return None, None
    m = VMWARE_DISK_KEY_REGEX.search(key_) or DISK_KEY_REGEX.search(key_)
    if not m:
        return None, None
    # O template oficial "Windows by Zabbix agent" usa o mount ENTRE ASPAS na
    # chave (ex.: vfs.fs.size["C:",pused]) — sem remover essas aspas, TODO
    # item Windows falha na normalização e o disco inteiro desaparece.
    mount = m.group(1).strip().strip('\'"')
    kind = (m.group(2) or '').lower() or None
    return mount, kind

# --------------------------------------------------------------------------
# SISTEMA OPERACIONAL DA VM
# --------------------------------------------------------------------------
# O item que carrega o SO no ambiente do cliente se chama SEMPRE
# "Sistema Operacional da VM" (nome pedido pelo cliente). Além do nome,
# reconhecemos também as CHAVES padrão do VMware/agente Zabbix como respaldo
# — assim o SO é detectado mesmo se o item for renomeado ou vier de outro
# template. O valor desse item é TEXTO (value_type 1/4), por isso ele é
# consultado à parte das métricas numéricas do relatório.
SO_NAME_REGEX = re.compile(r'sistema\s+operacional', re.IGNORECASE)
SO_KEY_REGEX = re.compile(
    r'(?:vmware\.vm\.guest\.osfullname|guest\.osfullname|system\.sw\.os|\bos(?:full)?name\b)',
    re.IGNORECASE
)

# Normalizador de ponto de montagem em nível de módulo (mesma regra da função
# do relatório): aceita disco Linux ('/...') ou drive Windows (uma letra).
# Fica aqui fora para o INVENTÁRIO da IA reaproveitar sem depender do closure
# interno de gerar_relatorio_consumo_todos.
def _norm_mount(mount):
    if not mount:
        return None
    m = mount.strip().strip('\'"')
    if m.startswith('/'):
        m = m.rstrip('/')
        return m or '/'
    wm = re.fullmatch(r'([A-Za-z]):?[\\/]?', m)
    if wm:
        return wm.group(1).upper() + ':'
    return None


def classificar_familia_so(texto):
    """Devolve 'Windows', 'Linux' ou None a partir do texto do SO da VM.
    Usado tanto para o ícone no cabeçalho do relatório quanto para a
    contagem de servidores Windows/Linux no inventário da IA."""
    if not texto:
        return None
    t = str(texto).lower()
    if 'windows' in t or 'microsoft' in t:
        return 'Windows'
    if any(x in t for x in (
        'linux', 'ubuntu', 'debian', 'centos', 'red hat', 'redhat', 'rhel',
        'suse', 'fedora', 'rocky', 'alma', 'oracle linux', 'freebsd', 'unix',
        'gnu/'
    )):
        return 'Linux'
    return None


def coletar_so_por_host(host_ids, token, url=None):
    """Busca o Sistema Operacional (texto) de cada host informado.

    Como o valor do SO é um item de TEXTO (value_type 1/4), ele NÃO vem nas
    consultas numéricas do relatório — por isso esta função faz uma coleta
    dedicada. Retorna {hostid: 'Microsoft Windows Server 2019', ...}.

    Nunca levanta exceção: o SO é um enriquecimento; se a coleta falhar, o
    relatório/inventário segue vivo sem ele (mostra 'SO não informado')."""
    so_por_host = {}
    if not host_ids:
        return so_por_host
    try:
        for lote in _chunks(list(host_ids), 50):
            itens = zabbix_api("item.get", {
                "hostids": lote,
                "output": ["hostid", "name", "key_", "lastvalue"],
                "filter": {"value_type": ["1", "4"]},  # 1=char, 4=text
            }, token, url)
            for it in itens:
                hid = it.get('hostid')
                if not hid or hid in so_por_host:
                    continue
                nome = it.get('name', '') or ''
                key_ = it.get('key_', '') or ''
                val = (it.get('lastvalue') or '').strip()
                if not val:
                    continue
                if SO_NAME_REGEX.search(nome) or SO_KEY_REGEX.search(key_):
                    so_por_host[hid] = val
    except Exception as e:
        print(f"[zabbix] Aviso: falha ao coletar SO dos hosts: {e}")
    return so_por_host


STATIC_DIR = os.path.join(os.getcwd(), 'app', 'static')
os.makedirs(STATIC_DIR, exist_ok=True)

# A MÁGICA COMEÇA AQUI: Recebe a URL do cliente, se não tiver, usa a padrão
def _chunks(lista, tamanho):
    """Divide uma lista em pedaços menores — usado pra não mandar UMA chamada
    gigante (todos os itens de dezenas de hosts de uma vez) pro Zabbix, que é
    o que estava causando timeout/resposta vazia em ambientes grandes."""
    for i in range(0, len(lista), tamanho):
        yield lista[i:i + tamanho]


class ZabbixConsultaPesadaError(Exception):
    """Sinaliza que o Zabbix não aguentou a consulta (timeout ou HTTP 5xx com
    resposta inválida/vazia). Quem chama pode reagir dividindo o lote em
    pedaços menores em vez de derrubar o relatório inteiro."""
    pass


def zabbix_api(method, params, token, url=None):
    if not token: raise ValueError("Token do Zabbix não configurado.")
    endpoint = url if url else ZABBIX_URL
    
    # 1. Tenta a forma tradicional do Zabbix 7.0 (Bearer Header)
    headers = {
        'Content-Type': 'application/json-rpc',
        'Authorization': f'Bearer {token}'
    }
    
    # 2. Injeta o token como "auth" no payload (Compatibilidade garantida Nginx/Apache)
    payload = {
        "jsonrpc": "2.0", 
        "method": method, 
        "params": params, 
        "auth": token, 
        "id": 1
    }
    
    import requests
    try:
        http_resp = requests.post(endpoint, json=payload, headers=headers, timeout=90)
    except requests.exceptions.Timeout:
        raise ZabbixConsultaPesadaError(
            f"O Zabbix não respondeu a tempo para '{method}' (ambiente grande demais para "
            f"uma única consulta?). Tente novamente ou reduza o período do relatório."
        )
    except requests.exceptions.RequestException as e:
        raise Exception(f"Falha de conexão com o Zabbix ao chamar '{method}': {e}")

    try:
        resposta = http_resp.json()
    except ValueError:
        # O Zabbix (ou um proxy/nginx na frente dele) devolveu algo que não é
        # JSON — normalmente um timeout do PHP ou uma página de erro HTML.
        # Isso é exatamente o que gerava o antigo "Expecting value: line 1
        # column 1 (char 0)", sem dizer o que realmente aconteceu.
        trecho = (http_resp.text or "")[:200].strip().replace("\n", " ")
        raise ZabbixConsultaPesadaError(
            f"O Zabbix devolveu uma resposta inválida para '{method}' "
            f"(HTTP {http_resp.status_code}){': ' + trecho if trecho else ' (corpo vazio)'}. "
            f"Provavelmente um timeout no servidor do Zabbix ao processar uma consulta grande."
        )

    if 'error' in resposta: 
        raise Exception(f"{resposta['error'].get('data')}")
    return resposta.get('result', [])
TAMANHO_LOTE = 50   # itens por chamada — 150 ainda estourava (HTTP 500 vazio)
                    # em ambientes grandes; 50 é o novo ponto de partida seguro.
LOTE_MINIMO = 5     # abaixo disso, para de dividir por itens e divide o PERÍODO.
JANELA_MINIMA = 6 * 3600  # não divide o período em janelas menores que 6h.


def _consulta_historico_adaptativa(method, params_base, itemids, time_from, time_till, token, url):
    """Consulta trend.get/history.get com resiliência: se o Zabbix estourar
    (timeout ou HTTP 5xx com corpo vazio — ZabbixConsultaPesadaError), divide o
    lote de itens ao meio e tenta de novo. Se mesmo com LOTE_MINIMO itens ainda
    falhar, divide o PERÍODO ao meio (consultas de janelas menores são muito
    mais leves pro banco do Zabbix). Só desiste de um pedaço quando não dá mais
    pra dividir nada — e mesmo assim devolve o que conseguiu, em vez de matar o
    relatório inteiro com uma exceção.

    Retorna (rows, itemids_perdidos)."""
    rows, perdidos = [], []
    # Fila de pendências: cada entrada é (lista_de_itemids, time_from, time_till)
    fila = [(list(itemids), time_from, time_till)]
    while fila:
        lote, tf, tt = fila.pop()
        try:
            rows.extend(zabbix_api(
                method,
                {**params_base, "itemids": lote, "time_from": tf, "time_till": tt},
                token, url
            ))
            # Respiro entre lotes: cede o GIL para as threads da interface web
            # (chat/logs/progresso) responderem, e alivia o servidor do Zabbix.
            time.sleep(0.05)
        except ZabbixConsultaPesadaError:
            time.sleep(0.5)  # dá um respiro pro servidor antes de re-tentar
            if len(lote) > LOTE_MINIMO:
                meio = len(lote) // 2
                fila.append((lote[:meio], tf, tt))
                fila.append((lote[meio:], tf, tt))
            elif (tt - tf) > JANELA_MINIMA:
                meio_t = (tf + tt) // 2
                fila.append((lote, tf, meio_t))
                fila.append((lote, meio_t + 1, tt))
            else:
                # Menor unidade possível ainda estoura: pula esses itens nessa
                # janela, mas o resto do relatório segue vivo.
                perdidos.extend(lote)
    return rows, perdidos


def fetch_historical_data(items_list, hora_passada, hora_atual, dias, token, url=None):
    hist_by_item = {}
    if not items_list: return hist_by_item

    by_type = {}
    for it in items_list:
        vt = int(it.get('value_type', 3))
        if vt not in by_type: by_type[vt] = []
        by_type[vt].append(it['itemid'])

    itens_perdidos = []

    for vt, iids in by_type.items():
        found_iids = set()
        if dias > 1:
            for lote in _chunks(iids, TAMANHO_LOTE):
                # "output" enxuto: o relatório só usa itemid/clock/value_avg.
                # Sem isso o trend.get devolve também num/value_min/value_max,
                # ~60% de payload a mais à toa — peso que contribuía pro 500.
                hist_data, perdidos = _consulta_historico_adaptativa(
                    "trend.get",
                    {"output": ["itemid", "clock", "value_avg"], "limit": 200000},
                    lote, hora_passada, hora_atual, token, url
                )
                itens_perdidos.extend(perdidos)
                for row in hist_data:
                    iid = row['itemid']
                    if iid not in hist_by_item: hist_by_item[iid] = []
                    hist_by_item[iid].append(row)
                    found_iids.add(iid)

        missing = [i for i in iids if i not in found_iids]
        for lote in _chunks(missing, TAMANHO_LOTE):
            hist_fallback, perdidos = _consulta_historico_adaptativa(
                "history.get",
                {"output": ["itemid", "clock", "value"], "history": vt, "limit": 200000},
                lote, hora_passada, hora_atual, token, url
            )
            itens_perdidos.extend(perdidos)
            for row in hist_fallback:
                iid = row['itemid']
                if iid not in hist_by_item: hist_by_item[iid] = []
                hist_by_item[iid].append(row)

    if itens_perdidos:
        # Não derruba o relatório: só registra no log do app quais itens o
        # Zabbix não conseguiu entregar nem na menor granularidade possível.
        print(f"[zabbix] Aviso: {len(set(itens_perdidos))} itens ficaram sem histórico "
              f"(Zabbix estourou mesmo nas menores consultas): {sorted(set(itens_perdidos))[:20]}")

    # trend.get devolvido em janelas pode vir fora de ordem cronológica;
    # ordena por clock pra os gráficos não ficarem com linhas "voltando no tempo".
    for iid in hist_by_item:
        hist_by_item[iid].sort(key=lambda r: int(r['clock']))

    return hist_by_item

def process_series(t_data, base_unit, item_name=""):
    t, v = [], []
    final_unit = base_unit.strip() if base_unit else ""
    unit_l = final_unit.lower()
    name_l = item_name.lower()
    
    for x in t_data:
        t.append(datetime.fromtimestamp(int(x['clock'])))
        raw_val = float(x.get('value_avg', x.get('value', 0)))
        
        if unit_l in ['hz', 'mhz']:
            v.append(raw_val / 1e9 if unit_l == 'hz' else raw_val / 1e3)
            final_unit = "GHz"
        elif unit_l in ['b', 'bytes', 'byte']:
            v.append(raw_val / (1024**3))
            final_unit = "GB"
        elif unit_l in ['kb', 'k']:
            v.append(raw_val / (1024**2))
            final_unit = "GB"
        elif unit_l in ['mb', 'm']:
            v.append(raw_val / 1024)
            final_unit = "GB"
        elif unit_l in ['bps', 'b/s', 'bits/s']:
            v.append(raw_val / 1e6)
            final_unit = "Mbps"
        elif unit_l in ['bps', 'bytes/s', 'b/s'] and 'bytes' in name_l:
            v.append((raw_val * 8) / 1e6)
            final_unit = "Mbps"
        elif not unit_l and re.search(r'\b(bytes?)\b', name_l):
            v.append((raw_val * 8) / 1e6)
            final_unit = "Mbps"
        elif not unit_l and re.search(r'\b(bits?|traffic|tráfego)\b', name_l):
            v.append(raw_val / 1e6)
            final_unit = "Mbps"
        elif '%' in unit_l:
            v.append(raw_val)
            final_unit = "%"
        else:
            if 'cpu' in name_l and raw_val <= 100.5:
                v.append(raw_val)
                final_unit = "%"
            elif 'mem' in name_l and raw_val > 1e6:
                v.append(raw_val / (1024**3))
                final_unit = "GB"
            elif any(x in name_l for x in ['user', 'usuário', 'utilizador', 'active', 'ativos']):
                v.append(raw_val)
                final_unit = "Usuários"
            else:
                v.append(raw_val)
                final_unit = final_unit if final_unit else ""
    return t, v, final_unit

def _formatar_duracao(segundos):
    """Converte segundos de uptime em texto humano ('3 dias, 4h')."""
    try:
        segundos = int(float(segundos))
    except (TypeError, ValueError):
        return "tempo desconhecido"
    if segundos < 0:
        return "tempo desconhecido"
    dias, resto = divmod(segundos, 86400)
    horas, resto = divmod(resto, 3600)
    minutos, _ = divmod(resto, 60)
    partes = []
    if dias: partes.append(f"{dias} dia{'s' if dias != 1 else ''}")
    if horas: partes.append(f"{horas}h")
    if not dias and minutos: partes.append(f"{minutos}min")
    return ", ".join(partes) if partes else "menos de 1 minuto"


def consultar_status_host_zabbix(nome_alvo: str, token: str, grupo: str, url: str = None) -> str:
    params = {"output": ["hostid", "host", "name", "status"], "selectInterfaces": ["ip", "dns", "useip"]}
    if nome_alvo and nome_alvo.lower() not in ['ambiente', 'meu ambiente', 'infraestrutura', 'tudo', 'geral']:
        params["search"] = {"name": nome_alvo}
    if grupo and grupo != "GLOBAL": 
        g_res = zabbix_api("hostgroup.get", {"search": {"name": grupo}}, token, url)
        if g_res: params["groupids"] = g_res[0].get('groupid')

    try:
        hosts = zabbix_api("host.get", params, token, url)
        if not hosts: return f"Nenhum servidor localizado no seu grupo ({grupo})."

        hostids = [h['hostid'] for h in hosts]

        # Busca o item real de UPTIME ("Tempo que a VM está online",
        # vmware.vm.guest.osuptime) em vez de confiar na flag host['status'],
        # que só diz se o MONITORAMENTO está habilitado na configuração do
        # Zabbix — não se a VM está de fato ligada e respondendo agora.
        uptime_por_host = {}
        try:
            itens_uptime = zabbix_api("item.get", {
                "hostids": hostids,
                "search": {"key_": "osuptime"},
                "output": ["hostid", "lastvalue", "lastclock"]
            }, token, url)
            for it in itens_uptime:
                uptime_por_host[it['hostid']] = it
        except Exception:
            pass  # segue sem uptime — cai no fallback pelo status de configuração

        agora = time.time()
        JANELA_DADOS_FRESCOS = 30 * 60  # 30 min sem coleta nova = dado "parado"/suspeito

        respostas = []
        for vm in hosts:
            conexao = "Monitoramento via API" 
            if vm.get('interfaces') and len(vm['interfaces']) > 0:
                interface = vm['interfaces'][0]
                if interface.get('useip') == '1' and interface.get('ip'): conexao = f"IP: {interface.get('ip')}"
                elif interface.get('useip') == '0' and interface.get('dns'): conexao = f"DNS: {interface.get('dns')}"

            item_up = uptime_por_host.get(vm['hostid'])
            if item_up and item_up.get('lastvalue') not in (None, ''):
                idade_dado = agora - int(item_up.get('lastclock') or 0)
                try:
                    segundos_uptime = float(item_up['lastvalue'])
                except (TypeError, ValueError):
                    segundos_uptime = None

                if idade_dado > JANELA_DADOS_FRESCOS:
                    status_str = f"⚠️ Sem dados recentes (última coleta há {int(idade_dado // 60)} min)"
                elif segundos_uptime is not None and segundos_uptime > 0:
                    status_str = f"🟢 Online há {_formatar_duracao(segundos_uptime)}"
                else:
                    status_str = "🔴 Desligada (uptime zerado)"
            else:
                # Sem item de uptime (ex.: firewalls/appliances não-VMware) —
                # cai no status de configuração, mas deixando claro que é só
                # "monitoramento habilitado", não confirmação de disponibilidade real.
                status_str = '🟢 Monitoramento ativo' if vm['status'] == '0' else '🔴 Monitoramento desativado'

            respostas.append(f"* **{vm['name']}** | {conexao} | Status: {status_str}\n\n")
        return f"### Status da Infraestrutura ({grupo}):\n\n" + "".join(respostas)
    except Exception as e: return f"Erro de comunicação com o Zabbix: {str(e)}"

def consultar_incidentes_zabbix(nome_alvo: str, dias_historico: float, token: str, grupo: str, search_term: str = "", url: str = None) -> str:
    dias = int(float(dias_historico)) if float(dias_historico) >= 1 else 7
    try:
        filtro = {}
        if grupo and grupo != "GLOBAL":
            grupos = zabbix_api("hostgroup.get", {"search": {"name": grupo}, "output": ["groupid"]}, token, url)
            if grupos: filtro["groupids"] = [g['groupid'] for g in grupos]
            
        if nome_alvo and nome_alvo.lower() not in ['ambiente', 'meu ambiente', 'infraestrutura', 'tudo', 'geral', 'vpn']:
            hosts = zabbix_api("host.get", {"search": {"name": nome_alvo}, "output": ["hostid"]}, token, url)
            if hosts: filtro["hostids"] = [h['hostid'] for h in hosts]
            else: return f"Servidor '{nome_alvo}' não encontrado."

        ativos = zabbix_api("problem.get", {**{"sortfield": ["eventid"], "sortorder": "DESC"}, **filtro}, token, url)
        recentes = zabbix_api("problem.get", {**{"recent": True, "time_from": int(time.time()) - (dias * 24 * 3600), "sortfield": ["eventid"], "sortorder": "DESC"}, **filtro}, token, url)

        problemas_dict = {p['eventid']: p for p in ativos}
        for p in recentes: problemas_dict.setdefault(p['eventid'], p)
        problemas = sorted(list(problemas_dict.values()), key=lambda x: int(x['clock']), reverse=True)

        t_ids = list(set([p['objectid'] for p in problemas if p.get('objectid')]))
        trigger_to_host = {}
        if t_ids:
            trigger_to_host = {t['triggerid']: t['hosts'][0]['name'] for t in zabbix_api("trigger.get", {"triggerids": t_ids, "selectHosts": ["name"]}, token, url) if t.get('hosts')}

        mapa_sev = {"0": "Info", "1": "Info", "2": "Atenção", "3": "Média", "4": "Alta", "5": "Crítica"}
        linhas = []
        for p in problemas:
            nome_incidente = p.get('name', 'Incidente')
            if search_term and search_term.lower() not in nome_incidente.lower(): continue
            status = '✅ RESOLVIDO' if p.get('r_eventid', '0') != '0' else '🚨 ATIVO'
            linhas.append(f"- **[{datetime.fromtimestamp(int(p['clock'])).strftime('%d/%m %H:%M')}]** {trigger_to_host.get(p.get('objectid'), 'Host')} | {status} ({mapa_sev.get(str(p.get('severity', '0')), 'Desc')}) -> {nome_incidente}\n\n")

        resultado_problemas = ""
        if not linhas:
            if search_term: resultado_problemas = f"🔍 **Busca Concluída:** Nenhum registro encontrado para o termo '**{search_term}**'."
            else: resultado_problemas = f"✅ O seu ambiente ({grupo}) está completamente saudável! Nenhum incidente registrado."
        else:
            resultado_problemas = f"### Incidentes Detectados ({grupo} - Últimos {dias} dias):\n\n" + "".join(linhas[:30])

        vpn_info = "\n\n### Status de Conexões VPN (Tempo Real):\n"
        try:
            items_vpn = zabbix_api("item.get", {
                "output": ["name", "lastvalue"],
                "selectHosts": ["name"],
                "search": {"name": ["VPN", "SSL"]},
                "searchByAny": True,
                **filtro
            }, token, url)
            
            conexoes = []
            for it in items_vpn:
                nl = it['name'].lower()
                if any(x in nl for x in ['user', 'usuário', 'active', 'ativos']):
                    val = float(it.get('lastvalue', 0))
                    if val > 0:
                        hname = it['hosts'][0]['name'] if it.get('hosts') else 'Desconhecido'
                        conexoes.append(f"- **{hname}**: {int(val)} usuários conectados ({it['name']})\n")
            
            if conexoes: vpn_info += "".join(conexoes)
            else: vpn_info += "- Nenhum usuário conectado nas VPNs neste exato momento.\n"
        except Exception as e:
            vpn_info += f"- Erro ao consultar VPNs em tempo real: {str(e)}\n"

        return resultado_problemas + vpn_info

    except Exception as e: return f"Erro na coleta de incidentes: {str(e)}"

def gerar_relatorio_consumo_todos(dias_historico: float, token: str, grupo: str, url: str = None, host_filter: str = None) -> str:
    """Gera o relatório completo (CPU/memória/rede/discos) para os hosts do
    grupo do usuário. Se host_filter for informado, restringe aos hosts cujo
    nome bate com esse filtro (usado pelo pedido de relatório de UMA VM
    específica) — mantendo o MESMO layout/motor do relatório de todos, só
    reduzindo a lista de hosts processados."""
    dias = int(float(dias_historico)) if float(dias_historico) >= 1 else 1
    hora_atual = int(time.time())
    hora_passada = int(hora_atual - (dias * 24 * 3600))
    metodo_hist = "trend.get" if dias > 1 else "history.get"
    
    try:
        filtro = {}
        if grupo and grupo != "GLOBAL":
            g_res = zabbix_api("hostgroup.get", {"search": {"name": grupo}}, token, url)
            if g_res: filtro["groupids"] = g_res[0].get('groupid')
        
        hosts = zabbix_api("host.get", {"output": ["hostid", "name"], **filtro}, token, url)
        if not hosts: return "Nenhum servidor encontrado no grupo."

        if host_filter:
            # IMPORTANTE: o filtro por nome atua SEMPRE sobre a lista já restrita
            # ao grupo do usuário (filtro acima) — nunca busca fora do escopo do
            # cliente, mesmo que o nome pedido exista em outra conta.
            alvo = host_filter.strip().lower()
            exatos = [h for h in hosts if h['name'].strip().lower() == alvo]
            if exatos:
                hosts = exatos
            else:
                parciais = [h for h in hosts if alvo in h['name'].strip().lower() or h['name'].strip().lower() in alvo]
                if not parciais:
                    return f"Não encontrei nenhuma VM chamada '{host_filter}' no seu ambiente. Confira o nome exato no Zabbix e tente novamente."
                hosts = parciais
        
        host_ids = [h['hostid'] for h in hosts]
        items = []
        for lote_hosts in _chunks(host_ids, 20):
            items.extend(zabbix_api("item.get", {
                "hostids": lote_hosts,
                "filter": {"status": "0", "value_type": ["0", "3"]},
                "output": ["itemid", "hostid", "name", "value_type", "units", "key_", "lastvalue", "lastclock"]
            }, token, url))

        # SO da VM (item de TEXTO — não vem na consulta numérica acima). Vai para
        # o cabeçalho de cada VM no relatório. Coleta tolerante a falha.
        so_por_host = coletar_so_por_host(host_ids, token, url)

        host_items = {h['hostid']: {'cpu': [], 'mem': [], 'nets': {}, 'vpn_users': [], 'disks': [], '_disk_map': {}, 'cpu_total': None, 'mem_total': None, 'uptime': None} for h in hosts}
        
        def is_valid_net(nl):
            return not re.search(r'\b(loopback|lo|veth|docker|speed|velocidade|drop|error|discard|status|estado|ssl)\b', nl)

        def _normalizar_mount(mount):
            """Aplica a regra estrita: só aceita disco Linux (começa com '/')
            ou drive Windows (uma letra A-Z, com ou sem ':'/'\\'). Qualquer
            outra coisa (nomes de item, texto livre, etc.) é REJEITADA — não
            tentamos mais adivinhar por frase/nome, só pela chave técnica.
            Devolve o identificador normalizado, ou None se não for um mount
            reconhecível por essa regra."""
            if not mount:
                return None
            m = mount.strip().strip('\'"')  # segunda camada: tira aspas residuais
            if m.startswith('/'):
                m = m.rstrip('/')
                return m or '/'
            # Windows: só uma letra, opcionalmente seguida de ':' e/ou '\' ou '/'
            wm = re.fullmatch(r'([A-Za-z]):?[\\/]?', m)
            if wm:
                return wm.group(1).upper() + ':'
            return None

        # Regex comprovado (vindo de um script de referência do próprio cliente
        # que já funciona no ambiente real deles): acha "C:" (drive Windows) ou
        # um caminho "/algo" (Linux) DENTRO do nome do item, não importa a frase
        # ao redor. Usado como respaldo quando a chave técnica não é suficiente
        # (muitos templates reais não seguem o padrão vfs.fs.size[...] limpo).
        _DISK_NAME_TOKEN_REGEX = re.compile(r'([A-Za-z]:|/[a-zA-Z0-9\-_/]*)')

        def try_classify_disk(it, nl, hid):
            """Classifica o item como disco. Duas fontes, nessa ordem:

            1) CHAVE técnica Zabbix (vfs.fs.size[MOUNT,KIND] / vfs.fs.dependent),
               quando reconhecível — mais robusta, independe de idioma.
            2) NOME do item, com o mesmo regex comprovado no ambiente real
               (letra de drive Windows ou caminho Linux embutido no texto).
               É o respaldo para os muitos templates cujos itens não usam a
               chave padrão do Zabbix.

            Em ambos os casos, MOUNT precisa ser um disco Linux ('/...') ou
            drive Windows (uma letra) — ver _normalizar_mount. Agrupa por
            disco: um MESMO disco físico pode ter vários itens (pused, pfree,
            used, free, total); guardamos TODOS os que existirem, e no
            momento de desenhar calculamos o uso com a melhor combinação
            disponível (ver _calcular_uso_disco).

            Devolve True se o item É de disco (para não vazar pro
            classificador de rede), mesmo que descartado por pseudo-fs."""
            mount, kind = _parse_disk_key(it.get('key_', ''))
            if mount is not None and kind in ('pused', 'pfree', 'used', 'free', 'total'):
                identificador = _normalizar_mount(mount)
                if identificador is not None and not DISCOS_BLOQUEADOS_REGEX.search(mount):
                    grupo = host_items[hid]['_disk_map'].setdefault(identificador, {})
                    grupo[kind] = it
                return True  # é item de disco pela chave (mesmo que descartado)

            # --- Fallback por nome ---
            if not any(x in nl for x in ('disco', 'disk', 'espaço em', 'espaco em', 'tamanho')):
                return False

            m = _DISK_NAME_TOKEN_REGEX.search(it['name'])
            if not m:
                return True  # é sobre disco, mas não achamos o identificador — ignora
            token = m.group(1)
            identificador = _normalizar_mount(token)
            if identificador is None or DISCOS_BLOQUEADOS_REGEX.search(token):
                return True

            unidades = (it.get('units') or '').lower()
            if '%' in unidades:
                if any(x in nl for x in ('livre', 'free')):
                    kind_estimado = 'pfree'
                else:
                    kind_estimado = 'pused'  # padrão mais comum quando ambíguo
            elif 'total' in nl:
                kind_estimado = 'total'
            elif any(x in nl for x in ('usado', 'used')):
                kind_estimado = 'used'
            elif any(x in nl for x in ('livre', 'free')):
                kind_estimado = 'free'
            else:
                return True  # não deu pra saber o tipo do valor, ignora com segurança

            grupo = host_items[hid]['_disk_map'].setdefault(identificador, {})
            # A chave técnica (quando existir) já tem prioridade automática, pois
            # roda primeiro no loop de items e "grupo[kind]" só é sobrescrito se
            # o MESMO kind aparecer de novo — então isso nunca derruba um dado
            # melhor que já tenha vindo pela chave.
            grupo.setdefault(kind_estimado, it)
            return True
        
        for it in items:
            nl = it['name'].lower()
            hid = it['hostid']
            if hid not in host_items: continue

            if 'cpu' in nl and re.search(r'\b(num|número|count|core)\b', nl): host_items[hid]['cpu_total'] = it
            elif re.search(r'\b(mem|memória|memory)\b', nl) and 'total' in nl and 'uso' not in nl: host_items[hid]['mem_total'] = it
            elif 'osuptime' in it.get('key_', '').lower(): host_items[hid]['uptime'] = it

            if 'cpu' in nl and re.search(r'\b(uso|usage|util|percent)\b', nl): host_items[hid]['cpu'].append(it)
            elif re.search(r'\b(mem|memória|memory)\b', nl) and re.search(r'\b(uso|usage|util|percent)\b', nl): host_items[hid]['mem'].append(it)
            elif re.search(r'\b(ssl|vpn|ipsec|tunnel|túnel)\b', nl) and re.search(r'\b(user|usuário|active|ativos)\b', nl):
                host_items[hid]['vpn_users'].append(it)
            elif try_classify_disk(it, nl, hid):
                pass  # já classificado (e adicionado, se não for pseudo-filesystem) dentro da função
            elif is_valid_net(nl) and re.search(r'\b(bits?|bytes?|net|traffic|tráfego|trafego|rate|bps|octets?)\b', nl):
                clean_name = re.sub(r'(?i)\b(in|out|inbound|outbound|rx|tx|receive[a-z]*|sent|transmit[a-z]*|recebidos|transmitidos|entrada|sa[ií]da|download|upload|traffic|tráfego|rate|bits?|bytes?|bps|octets?|per second)\b', '', it['name'])
                iface_name = re.sub(r'^[^\w]+|[^\w]+$', '', clean_name).strip(' -:[]()')
                if not iface_name: iface_name = "Principal"
                
                if iface_name not in host_items[hid]['nets']: host_items[hid]['nets'][iface_name] = {'in': None, 'out': None}
                
                if re.search(r'\b(in|inbound|rx|receive[a-z]*|recebidos|entrada|download)\b', nl):
                    host_items[hid]['nets'][iface_name]['in'] = it
                elif re.search(r'\b(out|outbound|tx|sent|transmit[a-z]*|transmitidos|sa[ií]da|upload)\b', nl):
                    host_items[hid]['nets'][iface_name]['out'] = it

        # Converte o _disk_map (agrupado por disco físico: {mount: {kind: item}})
        # na lista final 'disks' = [{'mount': mount, 'items': {kind: item}}, ...],
        # ordenada pelo mount para uma ordem estável e previsível (ex.: /, /boot, C:, D:).
        for hid in host_items:
            mapa = host_items[hid]['_disk_map']
            host_items[hid]['disks'] = [
                {'mount': mount, 'items': kinds}
                for mount, kinds in sorted(mapa.items(), key=lambda kv: kv[0])
            ]

        secoes_html_vms = []
        linhas_tabela_resumo = []
        max_cpu_global, max_mem_global = 0.0, 0.0
        vm_max_cpu, vm_max_mem = "N/A", "N/A"
        
        for h in hosts:
            hid, name = h['hostid'], h['name']
            h_it = host_items[hid]
            
            is_fgt = 'fortigate' in name.lower()
            
            host_items_to_fetch = []
            
            if not is_fgt:
                for k in ['cpu', 'mem']: host_items_to_fetch.extend(h_it[k])
                for grupo_disco in h_it['disks']:
                    host_items_to_fetch.extend(grupo_disco['items'].values())
                if h_it['cpu_total']: host_items_to_fetch.append(h_it['cpu_total'])
                if h_it['mem_total']: host_items_to_fetch.append(h_it['mem_total'])
            
            host_items_to_fetch.extend(h_it['vpn_users'])
            for pairs in h_it['nets'].values():
                if pairs['in']: host_items_to_fetch.append(pairs['in'])
                if pairs['out']: host_items_to_fetch.append(pairs['out'])
                
            hist_by_item = fetch_historical_data(host_items_to_fetch, hora_passada, hora_atual, dias, token, url)
            
            cpu_tot_val, mem_tot_val = "-", "-"
            if not is_fgt:
                cpu_tot_val, mem_tot_val = "Não Monitorado", "Não Monitorado"
                if h_it['cpu_total'] and h_it['cpu_total']['itemid'] in hist_by_item:
                    v = [float(x.get('value_avg', x.get('value', 0))) for x in hist_by_item[h_it['cpu_total']['itemid']]]
                    if v: cpu_tot_val = f"{int(max(v))} vCPUs"
                
                if h_it['mem_total'] and h_it['mem_total']['itemid'] in hist_by_item:
                    v = [float(x.get('value_avg', x.get('value', 0))) for x in hist_by_item[h_it['mem_total']['itemid']]]
                    if v: 
                        m_mx = max(v)
                        if m_mx > 1073741824: mem_tot_val = f"{m_mx/1073741824:.1f} GB"
                        elif m_mx > 1048576: mem_tot_val = f"{m_mx/1048576:.1f} MB"
                        else: mem_tot_val = f"{m_mx:.1f} GB"

            def get_best_item(item_list, preferred_words=None):
                if not item_list: return None
                candidates = item_list
                if preferred_words:
                    pref = [it for it in item_list if any(w in it['name'].lower() for w in preferred_words)]
                    if pref: candidates = pref
                best_it, max_avg = None, -1
                for it in candidates:
                    if it['itemid'] in hist_by_item:
                        vals = [float(x.get('value_avg', x.get('value', 0))) for x in hist_by_item[it['itemid']]]
                        if vals:
                            avg = sum(vals)/len(vals)
                            if avg > max_avg: max_avg = avg; best_it = it
                return best_it if best_it else candidates[0]

            best_cpu = get_best_item(h_it['cpu'], ['uso', 'usage', 'util', 'percent']) if not is_fgt else None
            best_mem = get_best_item(h_it['mem'], ['uso', 'usage', 'util', 'percent']) if not is_fgt else None
            best_vpn = get_best_item(h_it['vpn_users'], ['user', 'usuário', 'active', 'ativos'])

            net_list = []
            for iface, pairs in h_it['nets'].items():
                avg_in, avg_out = 0, 0
                if pairs['in'] and pairs['in']['itemid'] in hist_by_item:
                    v_in = [float(x.get('value_avg', 0)) for x in hist_by_item[pairs['in']['itemid']]]
                    if v_in: avg_in = sum(v_in)/len(v_in)
                if pairs['out'] and pairs['out']['itemid'] in hist_by_item:
                    v_out = [float(x.get('value_avg', 0)) for x in hist_by_item[pairs['out']['itemid']]]
                    if v_out: avg_out = sum(v_out)/len(v_out)
                
                if pairs['in'] or pairs['out']:
                    net_list.append({'iface': iface, 'pairs': pairs, 'score': max(avg_in, avg_out)})
            
            net_list.sort(key=lambda x: x['score'], reverse=True)
            top_nets = net_list[:6] if is_fgt else net_list[:2]

            line_plots = []
            if not is_fgt:
                if best_cpu: line_plots.append({'title': f'CPU - {name}', 'it_main': best_cpu, 'color': '#00e5ff', 'type': 'cpu'})
                if best_mem: line_plots.append({'title': f'Memória - {name}', 'it_main': best_mem, 'color': '#ff9e00', 'type': 'mem'})
            
            if best_vpn: line_plots.append({'title': f'Usuários Ativos (VPN/SSL) - {name}', 'it_main': best_vpn, 'color': '#00e676', 'type': 'vpn'})
            
            for i, n in enumerate(top_nets):
                title = f"Rede ({n['iface']}) - {name}" if is_fgt else (f"Rede Primária - {name}" if i==0 else f"Rede Secundária - {name}")
                line_plots.append({'title': title, 'it_in': n['pairs']['in'], 'it_out': n['pairs']['out'], 'type': 'net', 'is_primary': (i == 0)})

            line_rows = len(line_plots)
            # Sem limite artificial de quantidade: mostra TODOS os discos reais
            # do host (antes só entravam os 4 primeiros — era por isso que um
            # disco a mais, como o "S:", nunca aparecia no relatório).
            n_disks = len(h_it['disks']) if not is_fgt else 0
            disk_rows = 1 if n_disks > 0 else 0
            total_rows = max(1, line_rows + disk_rows)

            fig = Figure(figsize=(14, 4.5 * total_rows), facecolor='#1e1e1e')
            if total_rows > 0:
                gs = gridspec.GridSpec(total_rows, 1, figure=fig, hspace=0.5)

            c_str, m_str, ni_str, no_str, vpn_str, d_str_resumo, u_str = "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A"
            insights_finops = []

            # Uptime: usa o valor MAIS RECENTE (lastvalue), não uma média sobre o
            # período — uptime é um contador crescente, então a "média" ao longo de
            # 30 dias subestimaria bastante o tempo online atual da VM.
            item_uptime = h_it.get('uptime')
            if item_uptime and item_uptime.get('lastvalue') not in (None, ''):
                try:
                    segundos_uptime = float(item_uptime['lastvalue'])
                    u_str = _formatar_duracao(segundos_uptime) if segundos_uptime > 0 else "Desligada"
                except (TypeError, ValueError):
                    u_str = "N/A"

            def extract_and_plot(it, ax, plot_kw):
                if it and it['itemid'] in hist_by_item:
                    t, v, u = process_series(hist_by_item[it['itemid']], it.get('units', ''), it['name'])
                    if v:
                        if ax:
                            ax.plot(t, v, **plot_kw)
                            if 'linestyle' not in plot_kw: ax.fill_between(t, v, color=plot_kw.get('color', '#00e5ff'), alpha=0.1)
                        return sum(v)/len(v), max(v), u
                return None, None, ""

            def set_y_formatter(ax, unit):
                if unit == '%': ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, pos: f"{x:g}%"))
                elif unit: ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, pos: f"{x:g} {unit}"))

            for idx, lp in enumerate(line_plots):
                ax = fig.add_subplot(gs[idx])
                ax.set_facecolor('#121212')
                ax.tick_params(colors='#888888', labelsize=10)
                for spine in ax.spines.values(): spine.set_edgecolor('#333333')
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m %H:%M'))
                ax.grid(True, linestyle='--', alpha=0.1, color='#e0e0e0')
                ax.set_title(lp['title'], color=lp.get('color', '#00e5ff'), fontsize=13, fontweight='bold', loc='left', pad=12)

                if lp['type'] in ['cpu', 'mem', 'vpn']:
                    avg, mx, u = extract_and_plot(lp['it_main'], ax, {'color': lp['color'], 'linewidth': 1.8})
                    if avg is not None:
                        if lp['type'] == 'cpu':
                            u = '%' if not u and avg <= 100.5 else u
                            set_y_formatter(ax, u)
                            c_str = f"{avg:.1f}{u}" if u == '%' else f"{avg:.1f} {u}".strip()
                            if u == '%':
                                if mx > max_cpu_global: max_cpu_global = mx; vm_max_cpu = name
                                if not is_fgt and avg < 20: insights_finops.append("📉 <b>CPU (FinOps):</b> Ociosidade (Média < 20%). Avalie downsize.")
                                elif mx > 90: insights_finops.append("🚨 <b>CPU (Capacity):</b> Gargalo detectado (Picos > 90%). Upgrade recomendado.")
                        elif lp['type'] == 'mem':
                            u = '%' if not u and avg <= 100.5 else u
                            set_y_formatter(ax, u)
                            m_str = f"{avg:.1f}{u}" if u == '%' else f"{avg:.1f} {u}".strip()
                            if u == '%':
                                if mx > max_mem_global: max_mem_global = mx; vm_max_mem = name
                                if not is_fgt and avg < 30: insights_finops.append("📉 <b>Memória (FinOps):</b> Ociosidade. Possível downgrade.")
                                elif mx > 90: insights_finops.append("🚨 <b>Memória (Capacity):</b> Saturação > 90%. Risco de Swap.")
                        elif lp['type'] == 'vpn':
                            vpn_str = f"{avg:.0f} Usr (Pico: {mx:.0f})"
                            insights_finops.append(f"🌐 <b>VPN SSL:</b> Registrado pico de {mx:.0f} conexões simultâneas.")

                elif lp['type'] == 'net':
                    def net_fmt(x, pos): 
                        if x >= 1000: return f"{x/1000:g} Gbps"
                        elif x > 0 and x < 1: return f"{x*1000:g} Kbps"
                        elif x == 0: return "0"
                        return f"{x:g} Mbps"
                    
                    ax.yaxis.set_major_formatter(ticker.FuncFormatter(net_fmt))
                    
                    avg_in, mx_in, u_in = extract_and_plot(lp.get('it_in'), ax, {'color': '#b100e8', 'linewidth': 1.8, 'label': 'Download (In)'})
                    avg_out, mx_out, u_out = extract_and_plot(lp.get('it_out'), ax, {'color': '#00e5ff', 'linewidth': 1.8, 'label': 'Upload (Out)', 'linestyle': '--'})
                    if ax.get_legend_handles_labels()[0]: ax.legend(loc='upper left', fontsize=10, frameon=False, labelcolor='#e0e0e0')
                    
                    if lp.get('is_primary'):
                        if avg_in is not None: 
                            if avg_in >= 1000: ni_str = f"{avg_in/1000:.2f} Gbps"
                            elif avg_in > 0 and avg_in < 1: ni_str = f"{avg_in*1000:.0f} Kbps"
                            else: ni_str = f"{avg_in:.2f} Mbps"
                            
                        if avg_out is not None: 
                            if avg_out >= 1000: no_str = f"{avg_out/1000:.2f} Gbps"
                            elif avg_out > 0 and avg_out < 1: no_str = f"{avg_out*1000:.0f} Kbps"
                            else: no_str = f"{avg_out:.2f} Mbps"

            def _calcular_uso_disco(items_por_tipo):
                """Calcula o % de USO de um disco a partir dos itens disponíveis
                (pode ter 1 a 5 dos tipos: pused/pfree/used/free/total). Usa a
                melhor combinação possível, nessa ordem de preferência:
                1) pused direto  2) 100 - pfree  3) used/total  4) 100 - free/total
                5) used/(used+free)  — sem dado suficiente, devolve None (não 0,
                para diferenciar 'disco vazio de verdade' de 'sem dado')."""
                valores = {}
                for tipo, item in items_por_tipo.items():
                    avg, _mx, _u = extract_and_plot(item, None, {})
                    if avg is not None:
                        valores[tipo] = avg

                if 'pused' in valores:
                    return max(0.0, min(100.0, valores['pused']))
                if 'pfree' in valores:
                    return max(0.0, min(100.0, 100.0 - valores['pfree']))
                if 'used' in valores and 'total' in valores and valores['total'] > 0:
                    return max(0.0, min(100.0, valores['used'] / valores['total'] * 100.0))
                if 'free' in valores and 'total' in valores and valores['total'] > 0:
                    return max(0.0, min(100.0, 100.0 - (valores['free'] / valores['total'] * 100.0)))
                if 'used' in valores and 'free' in valores and (valores['used'] + valores['free']) > 0:
                    return max(0.0, min(100.0, valores['used'] / (valores['used'] + valores['free']) * 100.0))
                return None

            disk_summaries = []
            if n_disks > 0 and not is_fgt:
                gs_disk = gs[line_rows].subgridspec(1, n_disks, wspace=0.3)
                for i, grupo_disco in enumerate(h_it['disks']):
                    ax_d = fig.add_subplot(gs_disk[0, i])
                    ax_d.set_facecolor('#1e1e1e')

                    used_pct = _calcular_uso_disco(grupo_disco['items'])
                    sem_dado = used_pct is None
                    if sem_dado:
                        used_pct = 0.0

                    disk_summaries.append(f"{used_pct:.1f}%" if not sem_dado else "sem dado")
                    cor_uso = '#00e676' if not sem_dado else '#555555'
                    ax_d.pie([used_pct, max(0, 100 - used_pct)], labels=['Uso', 'Livre'], colors=[cor_uso, '#333333'], startangle=90, radius=1.2, wedgeprops=dict(width=0.4, edgecolor='#1e1e1e'), textprops={'color': '#aaaaaa', 'fontsize': 10})
                    texto_central = f'{used_pct:.1f}%' if not sem_dado else 's/ dado'
                    ax_d.text(0, 0, texto_central, ha='center', va='center', fontsize=13 if not sem_dado else 10, fontweight='bold', color='#ffffff')

                    # Título = SEMPRE o mount técnico (chave do Zabbix), nunca o nome
                    # do item — é por isso que fica limpo e consistente em qualquer
                    # idioma/template: "/", "/boot", "C:", "D:"...
                    n_amigavel = grupo_disco['mount']
                    ax_d.set_title(n_amigavel, fontsize=12, fontweight='bold', pad=20, color='#e0e0e0')

                    if not sem_dado and used_pct > 85:
                        insights_finops.append(f"🚨 <b>Disco ({n_amigavel}):</b> Volume crítico (> 85%). Expansão recomendada.")

            if disk_summaries: d_str_resumo = " | ".join(disk_summaries)
            
            if is_fgt: insights_finops.append("🛡️ <b>Fortigate (Firewall):</b> Monitoramento ativo das interfaces e túneis estabelecidos.")
            elif not insights_finops: insights_finops.append("✅ <b>Geral:</b> Dimensionamento adequado. Ativo operando dentro da normalidade (Health 100%).")
            
            insight_html = "<ul style='list-style-type: none; padding: 0; margin: 10px 0;'>" + "".join([f"<li style='margin-bottom: 8px;'>{i}</li>" for i in insights_finops]) + "</ul>"

            buf = io.BytesIO()
            if total_rows > 0:
                fig.savefig(buf, format='png', facecolor='#1e1e1e', edgecolor='none', bbox_inches='tight')
            buf.seek(0)
            img_b64 = base64.b64encode(buf.read()).decode('utf-8') if total_rows > 0 else ""
            plt.close(fig)
            # Respiro entre hosts: renderizar um gráfico matplotlib segura o GIL
            # por até alguns segundos. Em ambiente grande (100+ hosts), a sequência
            # sem pausa sufocava as threads do waitress e a aplicação inteira
            # parava de responder (Cloudflare 522). O sleep cede a vez às
            # requisições curtas da interface. NÃO altera o layout do relatório.
            time.sleep(0.05)

            vpn_html = f"<br>VPN Ativa: <strong style='color:#00e676'>{vpn_str}</strong>" if vpn_str != "N/A" else ""

            if is_fgt:
                resumo_card_html = f"""
                    <div style='flex: 1; min-width: 300px; font-family:monospace; background:#121212; padding:15px; border-radius:6px; font-size:14px; border: 1px solid #2a2a2a;'>
                        <strong style='color:#888;'>RESUMO DO FIREWALL:</strong><br><br>
                        Status: <strong style='color:#00e5ff'>Operacional</strong><br>
                        Túneis/Redes: <strong style='color:#ff9e00'>Monitoramento Ativo</strong><br>
                        Tráfego Principal: <strong style='color:#b100e8'>↓{ni_str} ↑{no_str}</strong>{vpn_html}
                    </div>
                """
                tab_resumo = f"<td style='color:#ccc; font-family: monospace;'>Equipamento de Rede (Firewall) | Tráfego: ↓{ni_str} ↑{no_str}</td>"
            else:
                resumo_card_html = f"""
                    <div style='flex: 1; min-width: 250px; background:#121212; padding:15px; border-radius:6px; font-size:14px; border: 1px solid #2a2a2a;'>
                        <strong style='color:#888;'>CAPACITY ALOCADO:</strong><br><br>
                        vCPUs: <strong style='color:#ffffff'>{cpu_tot_val}</strong><br>
                        Memória: <strong style='color:#ffffff'>{mem_tot_val}</strong>
                    </div>
                    <div style='flex: 1.5; min-width: 300px; font-family:monospace; background:#121212; padding:15px; border-radius:6px; font-size:14px; border: 1px solid #2a2a2a;'>
                        <strong style='color:#888;'>MÉDIAS DE CONSUMO:</strong><br><br>
                        CPU: <strong style='color:#00e5ff'>{c_str}</strong><br>
                        Memória: <strong style='color:#ff9e00'>{m_str}</strong><br>
                        Discos: <strong style='color:#00e676'>{d_str_resumo}</strong><br>
                        Rede Média: <strong style='color:#b100e8'>↓{ni_str} ↑{no_str}</strong>{f"<br>Uptime: <strong style='color:#3fb950'>{u_str}</strong>" if u_str != "N/A" else ""}
                    </div>
                """
                tab_resumo = f"<td style='color:#ccc; font-family: monospace;'>CPU: {c_str} | RAM: {m_str} | Disco: {d_str_resumo} | Rede: ↓{ni_str} ↑{no_str}</td>"

            img_tag = f"<img src='data:image/png;base64,{img_b64}' style='width:100%; border-radius:6px; border: 1px solid #2a2a2a;'>" if img_b64 else ""

            # ---- Selo de Sistema Operacional no cabeçalho da VM ----
            # Item "Sistema Operacional da VM" (coletado à parte, pois é texto).
            # Ícone por família: 🪟 Windows, 🐧 Linux, 💻 desconhecido/não informado.
            so_txt = so_por_host.get(hid)
            _fam = classificar_familia_so(so_txt)
            _ic = '🪟' if _fam == 'Windows' else ('🐧' if _fam == 'Linux' else '💻')
            _cor_so = '#00a2ff' if _fam == 'Windows' else ('#00e676' if _fam == 'Linux' else '#888')
            _so_label = so_txt if so_txt else 'SO não informado'
            so_badge_html = (
                f"<span style='display:inline-block; margin-left:12px; padding:3px 12px; "
                f"background:#121212; border:1px solid {_cor_so}; border-radius:14px; "
                f"font-size:13px; font-weight:600; color:{_cor_so}; vertical-align:middle; "
                f"font-family:sans-serif;'>{_ic} {_so_label}</span>"
            )

            secoes_html_vms.append(f"""
            <div id='vm_{hid}' style='background:#1e1e1e; padding:25px; border-radius:8px; margin-bottom:40px; border:1px solid #333; box-shadow: 0 4px 15px rgba(0,0,0,0.5);'>
                <h2 style='color:#ffffff; margin-top:0; border-bottom:1px solid #333; padding-bottom:10px;'>
                    <span class='status-dot green'></span> {name}{so_badge_html}
                </h2>
                <div style='display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 20px;'>
                    {resumo_card_html}
                    <div style='flex: 2; min-width: 400px; background:#16212b; padding:15px; border-radius:6px; border-left: 4px solid #00e5ff; font-size:14px;'>
                        <h4 style='margin: 0 0 10px 0; color: #00e5ff;'>🤖 Análise de Capacity & FinOps (Oráculo AI)</h4>
                        {insight_html}
                    </div>
                </div>
                {img_tag}
            </div>
            """)

            tab_so = f"<td style='color:{_cor_so}; font-size:13px; white-space:nowrap;'>{_ic} {_so_label}</td>"
            linhas_tabela_resumo.append(
                f"<tr><td><span class='status-dot green'></span></td>"
                f"<td><a href='#vm_{hid}' style='color:#00e5ff; font-weight:bold; text-decoration:none; transition: color 0.3s;' onmouseover=\"this.style.color='#ffffff'\" onmouseout=\"this.style.color='#00e5ff'\">{name}</a></td>"
                f"{tab_so}{tab_resumo}</tr>"
            )

        analise_texto = "A infraestrutura operou dentro dos limites normais de estabilidade. "
        if max_cpu_global > 85: analise_texto += f"Identificamos picos críticos de CPU atingindo <strong style='color:#ff4d4d'>{max_cpu_global:.1f}%</strong> na VM <strong>{vm_max_cpu}</strong>. "
        elif max_cpu_global > 0: analise_texto += f"O maior pico de CPU registrado no período foi de <strong style='color:#00e5ff'>{max_cpu_global:.1f}%</strong> na VM {vm_max_cpu}. "
        if max_mem_global > 85: analise_texto += f"A memória apresentou saturação crítica de <strong style='color:#ff4d4d'>{max_mem_global:.1f}%</strong> na VM <strong>{vm_max_mem}</strong>."

        html_filename = f"Dashboard_Executivo_{int(time.time())}.html"
        html_path = os.path.join(STATIC_DIR, html_filename)

        full_html = f"""<!DOCTYPE html><html lang='pt-br'><head><meta charset='UTF-8'><title>FinOps | {grupo}</title><style>
            body{{font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#121212; color:#e0e0e0; margin:0; padding:20px; scroll-behavior: smooth;}}
            .container{{max-width:1200px; margin:auto; background:#161616; padding:40px; border-radius:12px; box-shadow:0 10px 40px rgba(0,0,0,0.8); border: 1px solid #2a2a2a;}}
            h1{{text-align:center; color:#00e5ff; margin-bottom:10px; font-size:38px; font-weight:700; letter-spacing: 1px;}}
            .report-info{{text-align:center; color:#888; font-size:14px; margin-bottom:30px; padding-bottom:15px; border-bottom: 1px solid #333;}}
            .disclaimer-box{{background:#16212b; border-left:4px solid #00e5ff; padding:18px 25px; margin-bottom:40px; border-radius:6px; font-size:15px; line-height:1.6; color:#ccc; border: 1px solid #2a2a2a;}}
            .summary-table{{width:100%; border-collapse:collapse; margin-bottom:40px; border-radius: 8px; overflow: hidden; border: 1px solid #333;}}
            .summary-table th, .summary-table td{{padding:15px; border-bottom:1px solid #333; text-align:left;}}
            .summary-table th{{background:#242424; color:#00e5ff; font-weight: 600; text-transform: uppercase; font-size: 13px; letter-spacing: 1px;}}
            .summary-table tr:hover td{{background:#2a2a2a;}}
            .status-dot{{display:inline-block; width:14px; height:14px; border-radius:50%; margin-right:10px; vertical-align:middle;}}
            .green{{background:#00e676; box-shadow: 0 0 8px #00e676;}}
        </style></head><body><div class='container'>
        {branding.get_logo_html()}
        <h1>Dashboard Executivo & FinOps | {grupo}</h1>
        <div class='report-info'><strong>Período de Análise:</strong> Últimos {dias} dias<br><strong>Gerado em:</strong> {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>

        <div class='disclaimer-box'>
            <strong style='color:#00e5ff; font-size:16px;'>🤖 Resumo Global da IA:</strong><br>
            {analise_texto}
        </div>

        <table class='summary-table'><thead><tr><th>Status</th><th>Máquina Virtual (Clique para Detalhes)</th><th>Sistema Operacional</th><th>Métricas Médias no Período</th></tr></thead>
        <tbody>{"".join(linhas_tabela_resumo)}</tbody></table>

        <h2 style='color:#00e5ff; margin-top:50px; margin-bottom:20px; text-align:center; font-size: 28px;'>Análise de Capacity (AI) e Gráficos por Servidor</h2>
        {"".join(secoes_html_vms)}
        </div></body></html>"""

        with open(html_path, 'w', encoding='utf-8') as f: f.write(full_html)

        html_progress = (
            "<div style='margin-bottom: 15px; font-family: sans-serif;'>"
            "<div style='display: flex; justify-content: space-between; font-size: 13px; color: #aaa; margin-bottom: 5px;'>"
            "<span><strong style='color:#00e5ff;'>✓</strong> Processamento FinOps Concluído</span>"
            "<span style='color: #00e5ff; font-weight: bold;'>100%</span>"
            "</div>"
            "<div style='width:100%; background:#2a2a2a; border-radius:4px; overflow:hidden; border: 1px solid #333;'>"
            "<div style='height:8px; background: linear-gradient(90deg, #00e5ff, #0077ff); animation: progresso-oraculo 1s ease-out forwards;'></div>"
            "</div>"
            "</div>"
            "<style>@keyframes progresso-oraculo { 0% {width: 0%;} 100% {width: 100%;} }</style>"
        )

        msg_chat = "<p style='color:#00e5ff; font-weight:bold; font-size:15px; margin-bottom:10px;'>✅ Relatório Concluído</p>"

        botao_html = (
            f"<a href='/static/{html_filename}' download='{html_filename}' "
            "style='display: inline-block; background-color: #00e5ff; color: #121212; padding: 10px 20px; "
            "text-decoration: none; border-radius: 5px; font-family: sans-serif; font-weight: bold; font-size: 13px; "
            "border: 1px solid #00b4d8; margin-top: 5px; transition: background-color 0.3s;' "
            "onmouseover=\"this.style.backgroundColor='#00b4d8'\" onmouseout=\"this.style.backgroundColor='#00e5ff'\">"
            "📥 Baixar Relatório</a>"
        )

        return html_progress + msg_chat + botao_html
    except Exception as e: return f"Erro ao gerar relatório global: {str(e)}"

def gerar_grafico_zabbix(nome_host: str, metrica: str, quantidade_dias: float, token: str, grupo: str = None, url: str = None) -> str:
    """Relatório de UMA VM específica, pedida por nome (ex.: "gere relatório
    da VM CLIENTE_ASV_VM_ASV-BD01-IPEL"). Reaproveita o MESMO motor/layout do
    relatório de "TODOS" (gerar_relatorio_consumo_todos), só restringindo a
    lista de hosts a quem bate com o nome pedido — por isso o relatório sai
    completo (CPU, memória, rede, discos), não só a 'metrica' isolada.

    IMPORTANTE (multi-tenant): 'grupo' é sempre respeitado — a busca pelo nome
    do host NUNCA sai do grupo/conta do usuário que pediu o relatório, mesmo
    que exista uma VM com nome parecido em outra conta.
    """
    return gerar_relatorio_consumo_todos(quantidade_dias, token, grupo, url, host_filter=nome_host)




def _coletar_hosts_e_itens(token, grupo, url=None):
    """Coleta hosts do grupo do usuário e seus itens numéricos ativos.
    Helper COMPARTILHADO pelos módulos de análise (anomalias.py, capacidade.py).
    Mesmo padrão de escopo do relatório FinOps: NUNCA sai do grupo do cliente."""
    filtro = {}
    if grupo and grupo != "GLOBAL":
        g_res = zabbix_api("hostgroup.get", {"search": {"name": grupo}}, token, url)
        if g_res: filtro["groupids"] = g_res[0].get('groupid')

    hosts = zabbix_api("host.get", {"output": ["hostid", "name"], **filtro}, token, url)
    items = []
    for lote_hosts in _chunks([h['hostid'] for h in hosts], 20):
        items.extend(zabbix_api("item.get", {
            "hostids": lote_hosts,
            "filter": {"status": "0", "value_type": ["0", "3"]},
            "output": ["itemid", "hostid", "name", "value_type", "units", "key_", "lastvalue", "lastclock"]
        }, token, url))
    return hosts, items


def _fmt_ram_bytes(lv):
    """Formata um lastvalue de RAM total (em bytes) para GB/MB legível."""
    try:
        b = float(lv)
    except (TypeError, ValueError):
        return None
    if b <= 0:
        return None
    if b >= 1073741824:
        return f"{b/1073741824:.0f} GB"
    if b >= 1048576:
        return f"{b/1048576:.0f} MB"
    return f"{b:.0f} B"


def montar_inventario_para_ia(token, grupo, url=None):
    """Monta um INVENTÁRIO textual, ao vivo, de todas as VMs/hosts do grupo do
    usuário, para ALIMENTAR A IA DO CHAT. É o que permite o Oráculo responder
    perguntas como:
        - "Quais servidores são Windows?" (coluna Sistema Operacional)
        - "Quantas vCPUs tem cada VM?" (coluna vCPUs)
        - "Qual VM está consumindo mais CPU agora?" (coluna CPU agora)
        - "Tem algum gargalo no ambiente?" (CPU/memória/disco altos)

    Usa SOMENTE lastvalue (valor mais recente já coletado) — é rápido e leve,
    NÃO dispara history.get/trend.get, ao contrário do relatório FinOps. Assim
    pode rodar a cada mensagem do chat sem pesar. Respeita o multi-tenant: nunca
    sai do grupo/conta do usuário.

    Devolve uma string pronta para o contexto da IA, ou '' se não houver hosts.
    Nunca levanta exceção para fora (o chat não pode quebrar por causa disso)."""
    try:
        filtro = {}
        if grupo and grupo != "GLOBAL":
            g_res = zabbix_api("hostgroup.get", {"search": {"name": grupo}}, token, url)
            if g_res:
                filtro["groupids"] = g_res[0].get('groupid')

        hosts = zabbix_api("host.get", {"output": ["hostid", "name"], **filtro}, token, url)
        if not hosts:
            return ""

        host_ids = [h['hostid'] for h in hosts]
        nome_por_host = {h['hostid']: h['name'] for h in hosts}

        # SO (texto) — coleta dedicada (reaproveitada do relatório).
        so_por_host = coletar_so_por_host(host_ids, token, url)

        # Métricas numéricas (lastvalue) de todos os hosts.
        items = []
        for lote in _chunks(host_ids, 20):
            items.extend(zabbix_api("item.get", {
                "hostids": lote,
                "filter": {"status": "0", "value_type": ["0", "3"]},
                "output": ["itemid", "hostid", "name", "key_", "units", "lastvalue"]
            }, token, url))

        dados = {hid: {'vcpu': None, 'ram': None, 'cpu_uso': None,
                       'mem_uso': None, 'disks': {}} for hid in host_ids}

        def _num(lv):
            try:
                return float(lv)
            except (TypeError, ValueError):
                return None

        def _melhor_uso(atual, novo_val):
            # Mantém o MAIOR uso atual quando o host tem vários itens do mesmo
            # tipo (ex.: CPU por core + CPU total) — o pior caso é o que importa
            # para responder "quem está sobrecarregado".
            if novo_val is None:
                return atual
            if atual is None or novo_val > atual:
                return novo_val
            return atual

        # Sinais mais tolerantes que o do relatório: os templates OFICIAIS do
        # Zabbix usam "CPU utilization" e "Number of CPUs" — que os \bnum\b/\butil\b
        # estritos não pegavam. Aqui usamos prefixos (utiliz, núcleo) e, acima de
        # tudo, a UNIDADE '%' como sinal forte de "isto é uso percentual".
        _USO_REGEX = re.compile(r'(uso|usage|utiliz|util|percent|carga|load|ocupa)', re.IGNORECASE)
        _CORE_REGEX = re.compile(r'(núcleo|nucleo|core|cores|number|númer|numer|\bnum\b|count|quantidade|vcpu)', re.IGNORECASE)

        for it in items:
            hid = it.get('hostid')
            if hid not in dados:
                continue
            nl = (it.get('name') or '').lower()
            ul = (it.get('units') or '').lower()
            lv = it.get('lastvalue')
            eh_percentual = ('%' in ul) or bool(_USO_REGEX.search(nl))
            eh_cpu = 'cpu' in nl or 'processad' in nl or 'núcleo' in nl or 'nucleo' in nl
            eh_mem = bool(re.search(r'\b(mem|memória|memoria|memory|ram)\b', nl))

            # Uso atual de CPU (%): unidade % OU nome de "uso/utilization".
            if eh_cpu and eh_percentual and not _CORE_REGEX.search(nl):
                dados[hid]['cpu_uso'] = _melhor_uso(dados[hid]['cpu_uso'], _num(lv))
            # vCPUs alocadas (contagem de núcleos) — nunca percentual.
            elif eh_cpu and (not eh_percentual) and _CORE_REGEX.search(nl):
                n = _num(lv)
                if n is not None and 0 < n < 4096:
                    dados[hid]['vcpu'] = int(n)
            # Uso atual de memória (%).
            elif eh_mem and eh_percentual and 'total' not in nl:
                dados[hid]['mem_uso'] = _melhor_uso(dados[hid]['mem_uso'], _num(lv))
            # RAM total (bytes).
            elif eh_mem and 'total' in nl and 'uso' not in nl and not eh_percentual:
                ram = _fmt_ram_bytes(lv)
                if ram:
                    dados[hid]['ram'] = ram
            else:
                # Uso de disco por partição (% usado), via chave técnica.
                mount, kind = _parse_disk_key(it.get('key_', ''))
                if mount and kind in ('pused', 'pfree'):
                    ident = _norm_mount(mount)
                    if ident and not DISCOS_BLOQUEADOS_REGEX.search(mount):
                        v = _num(lv)
                        if v is not None:
                            usado = v if kind == 'pused' else max(0.0, 100.0 - v)
                            # fica com o maior uso visto para aquela partição
                            ant = dados[hid]['disks'].get(ident)
                            if ant is None or usado > ant:
                                dados[hid]['disks'][ident] = usado

        # ---- Monta o texto do inventário ----
        n_win = n_lin = n_outro = 0
        linhas = []
        for h in hosts:
            hid, name = h['hostid'], h['name']
            d = dados[hid]
            so = so_por_host.get(hid) or "SO não informado"
            familia = classificar_familia_so(so)
            if familia == 'Windows':
                n_win += 1
            elif familia == 'Linux':
                n_lin += 1
            else:
                n_outro += 1

            vcpu = f"{d['vcpu']}" if d['vcpu'] is not None else "n/d"
            ram = d['ram'] or "n/d"
            cpu = f"{d['cpu_uso']:.0f}%" if d['cpu_uso'] is not None else "n/d"
            mem = f"{d['mem_uso']:.0f}%" if d['mem_uso'] is not None else "n/d"
            if d['disks']:
                disco = ", ".join(f"{mp} {pct:.0f}%" for mp, pct in sorted(d['disks'].items()))
            else:
                disco = "n/d"

            linhas.append(
                f"| {name} | {so} | {vcpu} | {ram} | {cpu} | {mem} | {disco} |"
            )

        total = len(hosts)
        cabecalho = (
            "\n--- INVENTÁRIO DETALHADO DAS VMs (dados AO VIVO do Zabbix — lastvalue) ---\n"
            f"Total de hosts/VMs: {total} | Windows: {n_win} | Linux: {n_lin} | Outros/indefinidos: {n_outro}\n"
            "Colunas: VM | Sistema Operacional | vCPUs alocadas | RAM total | CPU agora | Memória agora | Disco usado por partição\n"
            "('n/d' = a métrica não é monitorada para essa VM no Zabbix)\n\n"
            "| VM | Sistema Operacional | vCPUs | RAM | CPU agora | Memória agora | Disco |\n"
            "|----|--------------------|-------|-----|-----------|---------------|-------|\n"
        )
        return cabecalho + "\n".join(linhas) + "\n"

    except Exception as e:
        # Enriquecimento opcional: se falhar, o chat segue com o contexto básico.
        print(f"[zabbix] Aviso: falha ao montar inventário para a IA: {e}")
        return ""
