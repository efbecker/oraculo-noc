import os
import re
import math
import time
import requests
import io
import base64
from datetime import datetime
from config import ZABBIX_URL

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

def _parse_disk_key(key_):
    """Retorna (mount_point, kind) a partir da key_ do item, ou (None, None) se a
    key não for reconhecida como item de espaço em disco do Zabbix."""
    if not key_:
        return None, None
    m = DISK_KEY_REGEX.search(key_)
    if not m:
        return None, None
    mount = m.group(1).strip()
    kind = (m.group(2) or '').lower() or None
    return mount, kind

STATIC_DIR = os.path.join(os.getcwd(), 'app', 'static')
os.makedirs(STATIC_DIR, exist_ok=True)

# A MÁGICA COMEÇA AQUI: Recebe a URL do cliente, se não tiver, usa a padrão
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
    resposta = requests.post(endpoint, json=payload, headers=headers, timeout=45).json()
    if 'error' in resposta: 
        raise Exception(f"{resposta['error'].get('data')}")
    return resposta.get('result', [])
def fetch_historical_data(items_list, hora_passada, hora_atual, dias, token, url=None):
    hist_by_item = {}
    if not items_list: return hist_by_item

    by_type = {}
    for it in items_list:
        vt = int(it.get('value_type', 3))
        if vt not in by_type: by_type[vt] = []
        by_type[vt].append(it['itemid'])
        
    for vt, iids in by_type.items():
        found_iids = set()
        if dias > 1:
            hist_data = zabbix_api("trend.get", {"itemids": iids, "time_from": hora_passada, "time_till": hora_atual, "limit": 200000}, token, url)
            for row in hist_data:
                iid = row['itemid']
                if iid not in hist_by_item: hist_by_item[iid] = []
                hist_by_item[iid].append(row)
                found_iids.add(iid)
        
        missing = [i for i in iids if i not in found_iids]
        if missing:
            hist_fallback = zabbix_api("history.get", {
                "itemids": missing, "history": vt, "time_from": hora_passada, "time_till": hora_atual, "limit": 200000
            }, token, url)
            for row in hist_fallback:
                iid = row['itemid']
                if iid not in hist_by_item: hist_by_item[iid] = []
                hist_by_item[iid].append(row)

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
        respostas = []
        for vm in hosts:
            conexao = "Monitoramento via API" 
            if vm.get('interfaces') and len(vm['interfaces']) > 0:
                interface = vm['interfaces'][0]
                if interface.get('useip') == '1' and interface.get('ip'): conexao = f"IP: {interface.get('ip')}"
                elif interface.get('useip') == '0' and interface.get('dns'): conexao = f"DNS: {interface.get('dns')}"
            status_str = '🟢 Online' if vm['status'] == '0' else '🔴 Offline/Inativo'
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

def gerar_relatorio_consumo_todos(dias_historico: float, token: str, grupo: str, url: str = None) -> str:
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
        
        host_ids = [h['hostid'] for h in hosts]
        items = zabbix_api("item.get", {
            "hostids": host_ids,
            "filter": {"status": "0", "value_type": ["0", "3"]},
            "output": ["itemid", "hostid", "name", "value_type", "units", "key_"]
        }, token, url)
        
        host_items = {h['hostid']: {'cpu': [], 'mem': [], 'nets': {}, 'vpn_users': [], 'disks': [], '_disk_map': {}, 'cpu_total': None, 'mem_total': None} for h in hosts}
        
        def is_valid_net(nl):
            return not re.search(r'\b(loopback|lo|veth|docker|speed|velocidade|drop|error|discard|status|estado|ssl)\b', nl)

        def try_classify_disk(it, nl, hid):
            """Tenta classificar o item como disco. Primeiro pela CHAVE técnica
            (robusto, independe de idioma/template); se não bater, cai no
            fallback por nome (heurística, para templates customizados).
            Devolve True se o item É de disco (mesmo que descartado pelo filtro
            de pseudo-filesystem), para não vazar pro classificador de rede.

            DEDUPLICA por disco: muitos templates do Zabbix têm 2-3 itens para
            o MESMO disco físico (ex.: bytes livres, bytes usados E percentual
            livre — vfs.fs.size[C:,free] / [C:,used] / [C:,pfree]). Sem isso,
            cada disco aparecia repetido várias vezes no relatório. Prioridade:
            item percentual (pused/pfree) da chave > qualquer outro.
            """
            mount, kind = _parse_disk_key(it.get('key_', ''))

            if mount is not None and kind in ('pused', 'pfree'):
                if not DISCOS_BLOQUEADOS_REGEX.search(mount):
                    identificador = mount.strip().upper()
                    existente = host_items[hid]['_disk_map'].get(identificador)
                    # Prioridade máxima: sempre pode substituir uma entrada de
                    # prioridade menor (bytes absolutos / nome) pela percentual.
                    if existente is None or existente[0] < 2:
                        host_items[hid]['_disk_map'][identificador] = (2, it)
                return True

            if mount is not None and kind in ('used', 'free', 'total'):
                # Item de disco reconhecido pela chave, mas em bytes absolutos
                # (não percentual). Só entra como candidato de prioridade baixa
                # — se depois aparecer a versão percentual do MESMO disco, ela
                # assume o lugar (tratado no bloco acima).
                if not DISCOS_BLOQUEADOS_REGEX.search(mount):
                    identificador = mount.strip().upper()
                    if identificador not in host_items[hid]['_disk_map']:
                        host_items[hid]['_disk_map'][identificador] = (1, it)
                return True

            if re.search(
                r'\b(espaço livre|espaco livre|espaço.*usado|espaco.*usado|pused|pfree|'
                r'space used|space free|storage used|disk used|disk free|disk usage|'
                r'space utilization|utilização|utilizacao|armazenamento|uso de disco)\b',
                nl
            ):
                n_amigavel = re.sub(
                    r'(?i)espaço livre no disco em porcentagem\s*(?:em\s+)?|espaco livre no disco em porcentagem\s*(?:em\s+)?|'
                    r'espaço em disco usado,?\s*em\s*%\s*(?:em\s+)?|espaco em disco usado,?\s*em\s*%\s*(?:em\s+)?',
                    '', it['name']
                ).strip()
                if not n_amigavel or not DISCOS_BLOQUEADOS_REGEX.search(n_amigavel):
                    # Sem key reconhecida: agrupa pelo nome "amigável" resultante
                    # (ex.: duas variações de texto que sobram em "C:" caem juntas).
                    identificador = (n_amigavel or it['name']).strip().upper()
                    if identificador not in host_items[hid]['_disk_map']:
                        host_items[hid]['_disk_map'][identificador] = (1, it)
                return True

            return False
        
        for it in items:
            nl = it['name'].lower()
            hid = it['hostid']
            if hid not in host_items: continue

            if 'cpu' in nl and re.search(r'\b(num|número|count|core)\b', nl): host_items[hid]['cpu_total'] = it
            elif re.search(r'\b(mem|memória|memory)\b', nl) and 'total' in nl and 'uso' not in nl: host_items[hid]['mem_total'] = it

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

        # Converte o _disk_map (deduplicado por disco, prioridade percentual >
        # bytes absolutos > nome) na lista final 'disks', ordenada pelo
        # identificador para uma ordem estável e previsível (ex.: C:, E:, F:, J:, S:).
        for hid in host_items:
            mapa = host_items[hid]['_disk_map']
            host_items[hid]['disks'] = [item for _, (prioridade, item) in sorted(mapa.items(), key=lambda kv: kv[0])]

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
                host_items_to_fetch.extend(h_it['disks'])
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

            c_str, m_str, ni_str, no_str, vpn_str, d_str_resumo = "N/A", "N/A", "N/A", "N/A", "N/A", "N/A"
            insights_finops = []

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

            disk_summaries = []
            if n_disks > 0 and not is_fgt:
                gs_disk = gs[line_rows].subgridspec(1, n_disks, wspace=0.3)
                for i, d_it in enumerate(h_it['disks']):
                    ax_d = fig.add_subplot(gs_disk[0, i])
                    ax_d.set_facecolor('#1e1e1e')
                    d_avg, d_max, d_u = extract_and_plot(d_it, None, {})
                    used_pct = 0.0
                    d_mount, d_kind = _parse_disk_key(d_it.get('key_', ''))
                    if d_avg is not None:
                        if d_u == '%' or d_avg <= 100.5:
                            if d_kind == 'pfree':
                                used_pct = 100.0 - d_avg
                            elif d_kind == 'pused':
                                used_pct = d_avg
                            else:
                                # Fallback (sem key reconhecida): heurística pelo nome.
                                used_pct = 100.0 - d_avg if any(x in d_it['name'].lower() for x in ['livre', 'free']) else d_avg
                        else: used_pct = 0.0

                    disk_summaries.append(f"{used_pct:.1f}%")
                    ax_d.pie([used_pct, max(0, 100-used_pct)], labels=['Uso', 'Livre'], colors=['#00e676', '#333333'], startangle=90, radius=1.2, wedgeprops=dict(width=0.4, edgecolor='#1e1e1e'), textprops={'color': '#aaaaaa', 'fontsize': 10})
                    ax_d.text(0, 0, f'{used_pct:.1f}%', ha='center', va='center', fontsize=13, fontweight='bold', color='#ffffff')
                    if d_mount:
                        n_amigavel = d_mount
                    else:
                        n_amigavel = re.sub(
                            r'(?i)espaço livre no disco em porcentagem\s*(?:em\s+)?|espaco livre no disco em porcentagem\s*(?:em\s+)?|'
                            r'espaço em disco usado,?\s*em\s*%\s*(?:em\s+)?|espaco em disco usado,?\s*em\s*%\s*(?:em\s+)?',
                            '', d_it['name']
                        ).strip()
                    ax_d.set_title(n_amigavel or 'Disco', fontsize=12, fontweight='bold', pad=20, color='#e0e0e0')
                    
                    if used_pct > 85: insights_finops.append(f"🚨 <b>Disco ({n_amigavel}):</b> Volume crítico (> 85%). Expansão recomendada.")

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
                        Rede Média: <strong style='color:#b100e8'>↓{ni_str} ↑{no_str}</strong>
                    </div>
                """
                tab_resumo = f"<td style='color:#ccc; font-family: monospace;'>CPU: {c_str} | RAM: {m_str} | Disco: {d_str_resumo} | Rede: ↓{ni_str} ↑{no_str}</td>"

            img_tag = f"<img src='data:image/png;base64,{img_b64}' style='width:100%; border-radius:6px; border: 1px solid #2a2a2a;'>" if img_b64 else ""

            secoes_html_vms.append(f"""
            <div id='vm_{hid}' style='background:#1e1e1e; padding:25px; border-radius:8px; margin-bottom:40px; border:1px solid #333; box-shadow: 0 4px 15px rgba(0,0,0,0.5);'>
                <h2 style='color:#ffffff; margin-top:0; border-bottom:1px solid #333; padding-bottom:10px;'>
                    <span class='status-dot green'></span> {name}
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

            linhas_tabela_resumo.append(
                f"<tr><td><span class='status-dot green'></span></td>"
                f"<td><a href='#vm_{hid}' style='color:#00e5ff; font-weight:bold; text-decoration:none; transition: color 0.3s;' onmouseover=\"this.style.color='#ffffff'\" onmouseout=\"this.style.color='#00e5ff'\">{name}</a></td>"
                f"{tab_resumo}</tr>"
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
        <h1>Dashboard Executivo & FinOps | {grupo}</h1>
        <div class='report-info'><strong>Período de Análise:</strong> Últimos {dias} dias<br><strong>Gerado em:</strong> {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>

        <div class='disclaimer-box'>
            <strong style='color:#00e5ff; font-size:16px;'>🤖 Resumo Global da IA:</strong><br>
            {analise_texto}
        </div>

        <table class='summary-table'><thead><tr><th>Status</th><th>Máquina Virtual (Clique para Detalhes)</th><th>Métricas Médias no Período</th></tr></thead>
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

def gerar_grafico_zabbix(nome_host: str, metrica: str, quantidade_dias: float, token: str, url: str = None) -> str:
    pass # Mantive essa parte limpa para você não perder nada que já tenha feito aqui!
