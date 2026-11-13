import os
import re
import math
import time
import requests
import io
import base64
import html as html_lib
from datetime import datetime
from app.settings import ZABBIX_URL
from app.services import branding
from app.services import report_store

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
    r'(?:vmware\.vm\.guest\.osfullname|guest\.osfullname|guestfullname|'
    r'config\.guestfullname|system\.sw\.os|\bos(?:full)?name\b)',
    re.IGNORECASE
)

# Hosts "agregadores/virtuais" que o cliente usa só no MAPA da infraestrutura
# (ex.: *_ALERTAS, *_SOMA_TRAFEGO_INTERFACES). Não são VMs reais — não têm SO,
# vCPU nem consumo próprio — então são EXCLUÍDOS do relatório e do inventário
# do chat, onde só poluiriam com "Não Monitorado / N/A".
HOSTS_AGREGADORES_REGEX = re.compile(
    r'(?:(?:^|[_\-\s])ALERTAS(?:[_\-\s]|$)|SOMA[_\-\s]?(?:DE[_\-\s]?)?TRAFEGO|'
    r'TRAFEGO[_\-\s]?INTERFACES|SOMA[_\-\s]?(?:DAS?[_\-\s]?)?INTERFACES)',
    re.IGNORECASE
)


# --------------------------------------------------------------------------
# QUANTIDADE DE vCPUs
# --------------------------------------------------------------------------
# Detecção pela CHAVE técnica (independe de idioma/nome do item):
#   - Linux  (agente):  system.cpu.num            -> "Quantidade de vCPUs"
#   - Windows (agente): wmi.get[root/cimv2,"Select NumberOfLogicalProcessors
#                       from Win32_ComputerSystem"] -> "Quantidade de vCPUs"
#   - VMware (API):     vmware.vm.cpu.num[...]
# Respaldo pelo NOME do item (ex.: "Quantidade de vCPUs", "Number of CPUs",
# "Número de núcleos"). Antes o relatório só olhava o nome com um regex que
# exigia "num/count/core" — "Quantidade de vCPUs" nunca casava e TODAS as VMs
# com agente Zabbix (templates CLIENTE_*_AGENTE_ATIVO) saíam "Não Monitorado".
VCPU_KEY_REGEX = re.compile(
    r'^(?:system\.cpu\.num\b|vmware\.vm\.cpu\.num\b|'
    r'wmi\.get\[.*NumberOf(?:Logical)?Processors)',
    re.IGNORECASE
)
VCPU_NAME_REGEX = re.compile(
    r'(?:\b(?:quantidade|qtd|qtde|n[uú]mero|numero|number|num|count|total)\b.*\b(?:v?cpus?|processadores|processors|n[uú]cleos|cores)\b'
    r'|\bv?cpus?\b.*\b(?:quantidade|qtd|qtde|n[uú]mero|number|num|count|cores?|n[uú]cleos)\b'
    r'|\bvcpus?\b)',
    re.IGNORECASE
)
# Palavras que indicam que o item é de USO/FREQUÊNCIA, não de contagem.
_VCPU_EXCLUI_REGEX = re.compile(
    r'(uso|usage|util|percent|porcentagem|carga|load|ready|wait|steal|idle|'
    r'interrup|switch|mhz|ghz|hz\b|tempo|time|kaspersky|process[oa]s?\b)',
    re.IGNORECASE
)


def eh_item_vcpu(it):
    """Diz se o item Zabbix é a QUANTIDADE de vCPUs da VM.
    Devolve 'key' quando reconhecido pela chave técnica (prioridade), 'name'
    quando só pelo nome (respaldo) e None se não for item de vCPU."""
    key_ = (it.get('key_') or '').strip()
    if VCPU_KEY_REGEX.search(key_):
        return 'key'
    nome = it.get('name') or ''
    units = (it.get('units') or '').lower()
    if '%' in units or 'hz' in units:
        return None
    if VCPU_NAME_REGEX.search(nome) and not _VCPU_EXCLUI_REGEX.search(nome):
        return 'name'
    return None


def _eh_host_agregador(nome):
    """True se o host for um agregador de mapa (Alertas / Soma de tráfego), que
    não deve entrar no relatório nem no inventário da IA."""
    return bool(nome and HOSTS_AGREGADORES_REGEX.search(nome))

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


def resumir_so(texto, limite=48):
    """Versão CURTA do SO para exibir no relatório (tabela e selo da VM).

    O item system.uname do Linux devolve a linha inteira do kernel — ex.:
    'Linux version 4.9.0-19-amd64 (debian-kernel@lists.debian.org) (gcc ...)
    #1 SMP Debian 4.9.320-2 (2022-06-30)' — que alargava a coluna e cortava
    o relatório. Aqui vira 'Debian · kernel 4.9.0-19'. No Windows, tira o
    hostname/build: 'Windows Server 2016 Standard x64'. O texto completo
    continua disponível no tooltip (title) de quem chama."""
    if not texto:
        return texto
    t = re.sub(r'\s+', ' ', str(texto)).strip()
    tl = t.lower()

    # Windows: pega a partir de "Microsoft Windows ..." / "Windows Server ..."
    m = re.search(r'(?:microsoft\s+)?(windows\s+(?:server\s+)?[\w.]+(?:\s+(?:r2|standard|datacenter|enterprise|pro|essentials|professional))*(?:\s+x64|\s+x86|\s+64-bit)?)', t, re.IGNORECASE)
    if ('windows' in tl or 'microsoft' in tl) and m:
        curto = m.group(1)
        # "Windows AZURE-AD 10.0.14393 Microsoft Windows Server 2016" -> usa o último
        todos = re.findall(r'windows\s+server\s+[\w.]+(?:\s+r2)?(?:\s+(?:standard|datacenter|enterprise|essentials))?(?:\s+x64|\s+x86)?', t, re.IGNORECASE)
        if todos:
            curto = todos[-1]
        return curto[:limite]

    # Linux (linha do uname / /proc/version)
    mk = re.match(r'linux version\s+(\S+)', t, re.IGNORECASE) or re.match(r'linux\s+\S+\s+(\d+\.\d+\S*)', t, re.IGNORECASE)
    if mk:
        kernel = mk.group(1)
        kcurto = re.match(r'(\d+\.\d+(?:\.\d+)?(?:-\d+)?)', kernel)
        kcurto = kcurto.group(1) if kcurto else kernel
        distro = None
        for padrao, nome in (
            (r'ubuntu', 'Ubuntu'), (r'debian', 'Debian'), (r'\.el(\d+)', 'RHEL/CentOS {0}'),
            (r'red hat', 'RHEL/CentOS'), (r'centos', 'CentOS'), (r'rocky', 'Rocky Linux'),
            (r'alma', 'AlmaLinux'), (r'suse', 'SUSE'), (r'fedora|\.fc(\d+)', 'Fedora'),
            (r'oracle|\.uek', 'Oracle Linux'), (r'amzn', 'Amazon Linux'),
        ):
            mm = re.search(padrao, tl)
            if mm:
                distro = nome.format(*[g for g in mm.groups() if g]) if '{0}' in nome and mm.groups() and mm.group(1) else nome.replace(' {0}', '')
                break
        return f"{distro or 'Linux'} · kernel {kcurto}"

    if len(t) > limite:
        return t[:limite - 1].rstrip() + '…'
    return t


def _prioridade_so(key_):
    """Menor = melhor. system.sw.os (agente Linux: 'Debian 9.13') > system.uname
    (agente; no Windows já é bem descritivo) > guestFullName do VMware (o que
    foi escolhido ao criar a VM — às vezes desatualizado, ex.: 'Debian 5')."""
    k = (key_ or '').lower()
    if k.startswith('system.sw.os'):
        return 0
    if k.startswith('system.uname'):
        return 1
    if k.startswith('vmware.'):
        return 3
    return 2


def coletar_so_por_host(host_ids, token, url=None):
    """Busca o Sistema Operacional (texto) de cada host informado.

    Como o valor do SO é um item de TEXTO (value_type 1/4), ele NÃO vem nas
    consultas numéricas do relatório — por isso esta função faz uma coleta
    dedicada. Retorna {hostid: 'Microsoft Windows Server 2019', ...}.

    Nunca levanta exceção: o SO é um enriquecimento; se a coleta falhar, o
    relatório/inventário segue vivo sem ele (mostra 'SO não informado')."""
    so_por_host = {}
    prioridade = {}
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
                if not hid:
                    continue
                nome = it.get('name', '') or ''
                key_ = it.get('key_', '') or ''
                val = (it.get('lastvalue') or '').strip()
                if not val:
                    continue
                if SO_NAME_REGEX.search(nome) or SO_KEY_REGEX.search(key_):
                    # Host com agente E VMware tem dois itens de SO. Fica o de
                    # maior prioridade (ver _prioridade_so), não o 1º que vier.
                    pr = _prioridade_so(key_)
                    if hid not in prioridade or pr < prioridade[hid]:
                        so_por_host[hid] = val
                        prioridade[hid] = pr
    except Exception as e:
        print(f"[zabbix] Aviso: falha ao coletar SO dos hosts: {e}")
    return so_por_host


STATIC_DIR = os.path.join(os.getcwd(), 'app', 'static')
os.makedirs(STATIC_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# PARÂMETROS DO CLUSTER / LAYOUT
# --------------------------------------------------------------------------
# No cluster VMware do cliente, cada vCPU equivale a 2 GHz de clock. É essa a
# base para calcular o % de uso de CPU: uso_GHz / (nº_vCPU * 2 GHz) * 100.
# Se o padrão do cluster mudar, basta ajustar aqui.
GHZ_POR_VCPU = 2.0

# Quantos "donuts" de disco cabem por LINHA no gráfico da VM. Acima disso o
# relatório quebra em várias linhas — antes tudo ia numa linha só e, com muitos
# discos, os nomes (ex.: /var/www/client4/web8/log) se sobrepunham/borravam.
DISCOS_POR_LINHA = 4


def _titulo_disco_curto(mount, limite=22):
    """Encurta nomes de ponto de montagem muito longos para o TÍTULO do donut,
    preservando o FINAL (a parte que distingue /.../web1/log de /.../web8/log).
    Ex.: '/var/www/client4/web8/log' -> '…/web8/log'. Discos curtos (C:, /boot)
    passam intactos. Só afeta o rótulo exibido; a chave real do disco é mantida
    nos insights e no resumo."""
    if not mount or len(mount) <= limite:
        return mount
    cauda = mount[-(limite - 1):]
    corte = cauda.find('/')
    if 0 < corte < len(cauda) - 1:
        cauda = cauda[corte:]
    return '…' + cauda


def _parse_gb(texto):
    """Extrai o valor em GB de uma string tipo '8.0 GB' / '512.0 MB' / '1.5 TB'.
    Devolve float (em GB) ou None."""
    if not texto:
        return None
    m = re.search(r'([\d.]+)\s*(TB|GB|MB)', str(texto), re.IGNORECASE)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).upper()
    if unit == 'TB':
        return val * 1024
    if unit == 'MB':
        return val / 1024
    return val


# --------------------------------------------------------------------------
# REGRAS DE RIGHT-SIZING (FinOps)
# --------------------------------------------------------------------------
# Folga aplicada sobre o PERCENTIL 95 do consumo ao recomendar vCPU/RAM.
FOLGA_RIGHTSIZING = 1.3
# Limiar de "CPU/memória sob pressão": o p95 do período passa disso, ou a VM
# fica acima dele em pelo menos PCT_TEMPO_PRESSAO% das amostras. Um pico
# isolado (ex.: backup/antivírus de madrugada) NÃO dispara mais o alerta.
LIMIAR_PRESSAO_CPU = 85.0
LIMIAR_PRESSAO_MEM = 90.0
PCT_TEMPO_PRESSAO = 5.0
# Pisos de dimensionamento (nunca recomendar abaixo disso).
MIN_VCPU = {'Windows': 2, 'Linux': 1}
MIN_RAM_GB = {'Windows': 4.0, 'Linux': 1.0}
# Corte máximo por rodada: no máximo metade do que está alocado. Reduções
# grandes (ex.: 8 -> 1) são feitas em etapas, reavaliando no relatório seguinte.
CORTE_MAXIMO = 0.5


# Previsão de enchimento de disco (regressão linear sobre o % usado).
MIN_PONTOS_PREVISAO = 48       # ~2 dias de trends horários
LIMIAR_CRESC_DISCO_DIA = 0.02  # < 0,02 pontos %/dia é ruído, não tendência
DIAS_ALERTA_DISCO = 60         # avisa discos que enchem em até 60 dias
# Uptime abaixo disso = "reinício recente".
REBOOT_RECENTE_S = 24 * 3600


def _regressao_linear(xs, ys):
    """Inclinação (unidade de y por unidade de x) por mínimos quadrados."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _rotulo_mount(mount):
    """'E:' -> 'E:', '/' -> '/:', '/var' -> '/var:' — rótulo curto do disco
    para 'C: 49%' nas tabelas (sem gerar 'E::')."""
    m = _titulo_disco_curto(mount or '?', 18)
    return m if m.endswith(':') else m + ':'


def _percentil(valores, p):
    """Percentil p (0-100) por interpolação linear. None se lista vazia."""
    if not valores:
        return None
    v = sorted(valores)
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * p / 100.0
    f = math.floor(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def _arredonda_ram_gb(gb):
    """Arredonda a RAM recomendada para um tamanho 'comprável': 0,5 GB abaixo
    de 4 GB, 1 GB até 16 GB e 2 GB acima disso — sempre para CIMA."""
    if gb <= 4:
        return math.ceil(gb * 2) / 2
    if gb <= 16:
        return float(math.ceil(gb))
    return float(math.ceil(gb / 2) * 2)


def _fmt_gb(gb):
    if gb is None:
        return "n/d"
    if gb < 1:
        return f"{gb*1024:.0f} MB"
    return f"{gb:.1f}".rstrip('0').rstrip('.') + " GB"


def _finops_capacity_insights(n_vcpu, capacidade_ghz, clock_ghz,
                              cpu_avg, cpu_peak, cpu_unit,
                              mem_avg, mem_peak, mem_unit, mem_total_gb,
                              cpu_p95=None, cpu_pct_alto=None,
                              mem_p95=None, mem_pct_alto=None, familia=None):
    """Recomendações FinOps de right-sizing por VM.

    Base do dimensionamento = PERCENTIL 95 do consumo (não o pico máximo, que
    pode ser um evento isolado) × FOLGA_RIGHTSIZING, respeitando os pisos por SO
    (MIN_VCPU / MIN_RAM_GB) e cortando no máximo CORTE_MAXIMO por rodada.
    Funciona com consumo em GHz (VMware) ou em % (agente).

    Devolve (insights_html, economia, alertas):
      economia = {'vcpu': nº de vCPUs devolvíveis, 'ram_gb': GB devolvíveis}
      alertas  = {'cpu_pressao': bool, 'mem_pressao': bool, 'cpu_pico_isolado': pico%}"""
    ins = []
    eco = {'vcpu': 0, 'ram_gb': 0.0}
    alertas = {'cpu_pressao': False, 'mem_pressao': False, 'cpu_pico_isolado': None}
    fam = familia if familia in MIN_VCPU else 'Linux'

    # ---------------- CPU ----------------
    cpu_u = (cpu_unit or '').lower()
    util_avg = util_peak = util_p95 = None
    cons_avg_ghz = cons_p95_ghz = None
    if cpu_avg is not None:
        if cpu_u == '%':
            util_avg, util_peak = cpu_avg, cpu_peak
            util_p95 = cpu_p95 if cpu_p95 is not None else cpu_peak
            if capacidade_ghz:
                cons_avg_ghz = util_avg / 100 * capacidade_ghz
                cons_p95_ghz = (util_p95 if util_p95 is not None else util_avg) / 100 * capacidade_ghz
        elif 'hz' in cpu_u:  # consumo em GHz (VMware)
            cons_avg_ghz = cpu_avg
            cons_p95_ghz = cpu_p95 if cpu_p95 is not None else cpu_peak
            if capacidade_ghz:
                util_avg = cons_avg_ghz / capacidade_ghz * 100
                util_p95 = (cons_p95_ghz if cons_p95_ghz is not None else cons_avg_ghz) / capacidade_ghz * 100
                util_peak = (cpu_peak / capacidade_ghz * 100) if cpu_peak is not None else None

    if util_avg is not None:
        p95 = util_p95 if util_p95 is not None else util_avg
        pressao = p95 >= LIMIAR_PRESSAO_CPU or (cpu_pct_alto or 0) >= PCT_TEMPO_PRESSAO
        p95_txt = f"{p95:.0f}%"
        pico_txt = f"{util_peak:.0f}%" if util_peak is not None else "n/d"
        if pressao:
            alertas['cpu_pressao'] = True
            tempo_txt = f", acima de {LIMIAR_PRESSAO_CPU:.0f}% em {cpu_pct_alto:.0f}% do tempo" if cpu_pct_alto else ""
            ins.append(f"🚨 <b>CPU sob pressão sustentada:</b> p95 de {p95_txt}{tempo_txt} (média {util_avg:.0f}%). "
                       f"Avalie AUMENTAR vCPUs" + (f" (hoje {n_vcpu})." if n_vcpu else "."))
        elif n_vcpu and capacidade_ghz and cons_p95_ghz is not None:
            piso = MIN_VCPU[fam]
            rec_ideal = max(1, math.ceil(cons_p95_ghz * FOLGA_RIGHTSIZING / clock_ghz), piso)
            rec = max(rec_ideal, math.ceil(n_vcpu * (1 - CORTE_MAXIMO)))
            rec = min(rec, n_vcpu)
            cons_txt = f"{cons_avg_ghz:.1f} de {capacidade_ghz:.0f} GHz" if cons_avg_ghz is not None else f"{util_avg:.0f}%"
            if rec < n_vcpu and util_avg < 35 and p95 < 70:
                eco['vcpu'] = n_vcpu - rec
                titulo = "Right-Sizing (CPU ociosa)" if util_avg < 15 else "CPU subutilizada"
                etapa = " Reavalie no próximo relatório para reduzir mais, se ainda houver folga." \
                    if rec_ideal < rec else ""
                ins.append(
                    f"🔻 <b>{titulo}:</b> média {util_avg:.0f}% da capacidade ({cons_txt}), p95 {p95_txt}, pico {pico_txt}. "
                    f"Recomenda-se reduzir de <b>{n_vcpu}</b> para <b>{rec}</b> vCPUs "
                    f"(cobre o p95 com {int((FOLGA_RIGHTSIZING-1)*100)}% de folga).{etapa}")
        elif cpu_u == '%' and util_avg < 20:
            ins.append("📉 <b>CPU (FinOps):</b> Ociosidade (média < 20%). Sem contagem de vCPUs para dimensionar.")
        if not pressao and util_peak is not None and util_peak > 90:
            alertas['cpu_pico_isolado'] = util_peak
            ins.append(f"ℹ️ <b>Pico isolado de CPU:</b> {pico_txt} em algum momento, mas p95 de só {p95_txt} — "
                       f"não indica falta de CPU. Vale checar rotinas agendadas (backup, antivírus, updates).")

    # ---------------- Memória ----------------
    mem_u = (mem_unit or '').lower()
    m_util_avg = m_util_p95 = m_util_peak = None
    m_avg_gb = m_p95_gb = None
    if mem_avg is not None:
        if mem_u == '%':
            m_util_avg, m_util_peak = mem_avg, mem_peak
            m_util_p95 = mem_p95 if mem_p95 is not None else mem_peak
            if mem_total_gb:
                m_avg_gb = m_util_avg / 100 * mem_total_gb
                m_p95_gb = (m_util_p95 if m_util_p95 is not None else m_util_avg) / 100 * mem_total_gb
        elif 'gb' in mem_u or 'mb' in mem_u or mem_u == 'b':  # consumo em GB (VMware)
            m_avg_gb = mem_avg
            m_p95_gb = mem_p95 if mem_p95 is not None else mem_peak
            if mem_total_gb:
                m_util_avg = m_avg_gb / mem_total_gb * 100
                m_util_p95 = (m_p95_gb if m_p95_gb is not None else m_avg_gb) / mem_total_gb * 100
                m_util_peak = (mem_peak / mem_total_gb * 100) if mem_peak is not None else None

    if m_util_avg is not None:
        p95m = m_util_p95 if m_util_p95 is not None else m_util_avg
        pressao_m = p95m >= LIMIAR_PRESSAO_MEM or (mem_pct_alto or 0) >= PCT_TEMPO_PRESSAO
        if pressao_m:
            alertas['mem_pressao'] = True
            ins.append(f"🚨 <b>Memória saturada:</b> p95 de {p95m:.0f}% (média {m_util_avg:.0f}%"
                       + (f" de {_fmt_gb(mem_total_gb)}" if mem_total_gb else "") + "). Risco de swap — avalie AUMENTAR a RAM.")
        elif mem_total_gb and m_p95_gb is not None:
            piso = MIN_RAM_GB[fam]
            rec_gb = _arredonda_ram_gb(m_p95_gb * FOLGA_RIGHTSIZING)
            rec_gb = max(rec_gb, piso, _arredonda_ram_gb(mem_total_gb * (1 - CORTE_MAXIMO)))
            # Só recomenda se a redução for relevante (>= 1 GB e >= 20% do total).
            if m_util_avg < 35 and rec_gb <= mem_total_gb - 1 and rec_gb <= mem_total_gb * 0.8:
                eco['ram_gb'] = mem_total_gb - rec_gb
                gb_txt = f"{_fmt_gb(m_avg_gb)} de {_fmt_gb(mem_total_gb)}" if m_avg_gb is not None else f"{m_util_avg:.0f}%"
                ins.append(
                    f"🔻 <b>Right-Sizing (RAM ociosa):</b> uso médio de {gb_txt} ({m_util_avg:.0f}%), p95 {p95m:.0f}%. "
                    f"Avalie reduzir de <b>{_fmt_gb(mem_total_gb)}</b> para <b>{_fmt_gb(rec_gb)}</b>.")
        elif mem_u == '%' and m_util_avg < 30:
            ins.append("📉 <b>Memória (FinOps):</b> Ociosidade. Sem RAM total para dimensionar.")

    return ins, eco, alertas

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

def _texto_puro(html_txt):
    """Remove tags/emoji-markup de um insight para mandar à IA como texto."""
    t = re.sub(r'<[^>]+>', '', html_txt or '')
    return html_lib.unescape(re.sub(r'\s+', ' ', t)).strip()


def _md_simples_para_html(texto):
    """Converte a resposta da IA (texto com **negrito** e listas '- ') em HTML
    seguro: escapa tudo antes, então a IA nunca injeta HTML no relatório."""
    linhas_html, em_lista = [], False
    for linha in (texto or '').strip().splitlines():
        l = linha.strip()
        if not l:
            if em_lista:
                linhas_html.append('</ul>'); em_lista = False
            continue
        l = re.sub(r'^#+\s*', '', l)
        esc = html_lib.escape(l)
        esc = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', esc)
        m = re.match(r'^(?:[-*•]|\d+[.)])\s+(.*)', esc)
        if m:
            if not em_lista:
                linhas_html.append("<ul style='margin:8px 0; padding-left:20px;'>"); em_lista = True
            linhas_html.append(f"<li style='margin-bottom:6px;'>{m.group(1)}</li>")
        else:
            if em_lista:
                linhas_html.append('</ul>'); em_lista = False
            linhas_html.append(f"<p style='margin:8px 0;'>{esc}</p>")
    if em_lista:
        linhas_html.append('</ul>')
    return ''.join(linhas_html)


def _resumo_executivo_ia(username, grupo, dias, n_vms, pontos_html, tot_vcpu, tot_ram,
                         economia_vms, insights_por_vm):
    """Pede à IA configurada para o usuário (Admin > IA) um resumo executivo
    do relatório. Devolve (html, rótulo_da_ia) ou (None, None) se a IA não
    estiver configurada ou falhar — nunca derruba o relatório."""
    try:
        from app.services import ai_providers
        cfg = ai_providers.config_for_user(username or '')
        if not (cfg.get('provider') or '').strip():
            return None, None

        linhas = [f"Cliente: {grupo} | Período: últimos {dias} dias | VMs analisadas: {n_vms}"]
        linhas.append("Pontos de atenção calculados pelas regras:")
        linhas += [f"- {_texto_puro(p)}" for p in pontos_html] or ["- nenhum"]
        linhas.append(f"Oportunidade total de right-sizing: {tot_vcpu} vCPUs e {_fmt_gb(tot_ram)} de RAM "
                      f"em {len(economia_vms)} VMs.")
        top = sorted(economia_vms, key=lambda e: (e['vcpu'], e['ram_gb']), reverse=True)[:8]
        if top:
            linhas.append("Maiores oportunidades (VM: vCPUs / RAM devolvíveis):")
            linhas += [f"- {e['name']}: {e['vcpu'] or '—'} vCPU / {_fmt_gb(e['ram_gb']) if e['ram_gb'] else '—'} RAM" for e in top]
        linhas.append("Achados por VM:")
        for nome, ins in insights_por_vm:
            if ins:
                linhas.append(f"* {nome}: " + " | ".join(ins))
        dados = "\n".join(linhas)
        if len(dados) > 24000:
            dados = dados[:24000] + "\n[...]"

        preamble = (
            "Você é um analista sênior de FinOps e capacidade de infraestrutura (VMware/Zabbix). "
            "Escreva em português do Brasil, em tom executivo, para o gestor de TI do cliente. "
            "Use SOMENTE os números e fatos fornecidos; não invente valores, VMs nem causas. "
            "Não repita a lista inteira de achados: sintetize e priorize.")
        prompt = (
            "Com base nos dados abaixo, escreva:\n"
            "1) Um parágrafo de 3 a 4 frases com a situação geral do ambiente (riscos e oportunidade de economia).\n"
            "2) Uma lista de 3 a 5 prioridades, da mais urgente para a menos urgente, cada uma com a ação "
            "recomendada e as VMs envolvidas. Riscos operacionais (disco cheio, CPU/memória sob pressão) vêm "
            "antes de economia.\n"
            "Formato: texto simples; use '- ' para os itens da lista e **negrito** só para nomes de VMs. "
            "Sem títulos, sem tabelas.\n\n"
            f"DADOS:\n{dados}")
        texto = ai_providers.chat(prompt, preamble, [], cfg=cfg, max_tokens=1500)
        if not (texto or '').strip():
            return None, None
        return _md_simples_para_html(texto), ai_providers.label_for_user(username or '')
    except Exception as e:
        print(f"[zabbix] Resumo executivo por IA indisponível, usando só as regras: {e}")
        return None, None


def gerar_relatorio_consumo_todos(dias_historico: float, token: str, grupo: str, url: str = None, host_filter: str = None, criado_por: str = None, clock_vcpu: float = None) -> str:
    """Gera o relatório completo (CPU/memória/rede/discos) para os hosts do
    grupo do usuário. Se host_filter for informado, restringe aos hosts cujo
    nome bate com esse filtro (usado pelo pedido de relatório de UMA VM
    específica) — mantendo o MESMO layout/motor do relatório de todos, só
    reduzindo a lista de hosts processados."""
    dias = int(float(dias_historico)) if float(dias_historico) >= 1 else 1
    hora_atual = int(time.time())
    hora_passada = int(hora_atual - (dias * 24 * 3600))
    metodo_hist = "trend.get" if dias > 1 else "history.get"

    # Clock (GHz) de cada vCPU no cluster deste cliente. Vem do cadastro do
    # usuário (Admin > Usuários); se não informado, usa o padrão do sistema.
    # Base da capacidade: capacidade_GHz = nº_vCPUs × clock_ghz.
    try:
        clock_ghz = float(clock_vcpu) if clock_vcpu and float(clock_vcpu) > 0 else GHZ_POR_VCPU
    except (TypeError, ValueError):
        clock_ghz = GHZ_POR_VCPU
    
    try:
        filtro = {}
        if grupo and grupo != "GLOBAL":
            g_res = zabbix_api("hostgroup.get", {"search": {"name": grupo}}, token, url)
            if g_res: filtro["groupids"] = g_res[0].get('groupid')
        
        hosts = zabbix_api("host.get", {"output": ["hostid", "name"], **filtro}, token, url)
        if not hosts: return "Nenhum servidor encontrado no grupo."

        # Remove hosts agregadores de mapa (Alertas / Soma de tráfego): não são
        # VMs reais e só apareceriam no relatório como "Não Monitorado".
        hosts = [h for h in hosts if not _eh_host_agregador(h.get('name', ''))]
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

        # Cada host pode ter DUAS fontes ao mesmo tempo: o agente Zabbix (dentro
        # do SO) e o template VMware (API do vCenter). Rede e discos de cada fonte
        # ficam em mapas SEPARADOS (_vmw) e, no fim, usamos UMA fonte por métrica
        # — nunca somamos/mesclamos as duas (ver 'Escolha de fonte' abaixo).
        host_items = {h['hostid']: {'cpu': [], 'mem': [], 'nets': {}, 'nets_vmw': {}, 'vpn_users': [], 'disks': [],
                                    '_disk_map': {}, '_disk_map_vmw': {}, 'cpu_total': None, 'mem_total': None,
                                    'mem_total_vmw': None, 'uptime': None, 'uptime_vmw': None, 'power': None}
                      for h in hosts}

        def _eh_vmware(it):
            return (it.get('key_') or '').strip().lower().startswith('vmware.')
        
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
                    grupo = host_items[hid]['_disk_map_vmw' if _eh_vmware(it) else '_disk_map'].setdefault(identificador, {})
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

            grupo = host_items[hid]['_disk_map_vmw' if _eh_vmware(it) else '_disk_map'].setdefault(identificador, {})
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

            key_l = (it.get('key_') or '').strip().lower()

            # Quantidade de vCPUs: CHAVE técnica primeiro (system.cpu.num no
            # Linux, wmi NumberOfLogicalProcessors no Windows, vmware.vm.cpu.num
            # no VMware) e NOME como respaldo ("Quantidade de vCPUs" etc.). Um
            # item achado pela chave nunca é substituído por um achado pelo nome.
            origem_vcpu = eh_item_vcpu(it)
            if origem_vcpu:
                if host_items[hid]['cpu_total'] is None or (
                        origem_vcpu == 'key' and host_items[hid].get('_cpu_total_origem') != 'key'):
                    host_items[hid]['cpu_total'] = it
                    host_items[hid]['_cpu_total_origem'] = origem_vcpu
                continue  # item de contagem: não é uso de CPU, disco nem rede

            # Estado da VM no vCenter (0 desligada, 1 ligada, 2 suspensa).
            if key_l.startswith('vmware.vm.powerstate['):
                host_items[hid]['power'] = it
                continue

            # Memória total: RAM ALOCADA no VMware (vmware.vm.memory.size[...]) é
            # guardada à parte — é o que o cliente paga e tem prioridade na
            # capacidade. Do agente, vm.memory.size[total]; nome como respaldo.
            if key_l.startswith('vmware.vm.memory.size['):
                host_items[hid]['mem_total_vmw'] = it
                continue
            if key_l.startswith('vm.memory.size[total]'):
                host_items[hid]['mem_total'] = it
                host_items[hid]['_mem_total_origem'] = 'key'
                continue
            elif re.search(r'\b(mem|memória|memory)\b', nl) and 'total' in nl and 'uso' not in nl:
                if host_items[hid].get('_mem_total_origem') != 'key':
                    host_items[hid]['mem_total'] = it
            elif 'vmware.vm.guest.osuptime' in key_l:
                host_items[hid]['uptime_vmw'] = it
                continue
            elif 'osuptime' in key_l or key_l.startswith('system.uptime'):
                host_items[hid]['uptime'] = it
                continue

            if 'cpu' in nl and re.search(r'\b(uso|usage|util|percent)\b', nl): host_items[hid]['cpu'].append(it)
            elif re.search(r'\b(mem|memória|memory)\b', nl) and re.search(r'\b(uso|usage|util|percent)\b', nl): host_items[hid]['mem'].append(it)
            elif re.search(r'\b(ssl|vpn|ipsec|tunnel|túnel)\b', nl) and re.search(r'\b(user|usuário|active|ativos)\b', nl):
                host_items[hid]['vpn_users'].append(it)
            elif try_classify_disk(it, nl, hid):
                pass  # já classificado (e adicionado, se não for pseudo-filesystem) dentro da função
            elif is_valid_net(nl) and re.search(r'\b(bits?|bytes?|net|traffic|tráfego|trafego|rate|bps|octets?)\b', nl):
                clean_name = re.sub(r'(?i)\b(in|out|inbound|outbound|rx|tx|receive[a-z]*|sent|transmit[a-z]*|recebidos|transmitidos|enviados|entrada|sa[ií]da|download|upload|traffic|tráfego|rate|bits?|bytes?|bps|octets?|per second)\b', '', it['name'])
                iface_name = re.sub(r'^[^\w]+|[^\w]+$', '', clean_name).strip(' -:[]()')
                if not iface_name: iface_name = "Principal"
                
                nets = host_items[hid]['nets_vmw' if _eh_vmware(it) else 'nets']
                if iface_name not in nets: nets[iface_name] = {'in': None, 'out': None}

                if re.search(r'\b(in|inbound|rx|receive[a-z]*|recebidos|entrada|download)\b', nl):
                    nets[iface_name]['in'] = it
                elif re.search(r'\b(out|outbound|tx|sent|transmit[a-z]*|transmitidos|enviados|sa[ií]da|upload)\b', nl):
                    nets[iface_name]['out'] = it

        # Converte o _disk_map (agrupado por disco físico: {mount: {kind: item}})
        # na lista final 'disks' = [{'mount': mount, 'items': {kind: item}}, ...],
        # ordenada pelo mount para uma ordem estável e previsível (ex.: /, /boot, C:, D:).
        # ---- Escolha de fonte (agente x VMware) — sem misturar ----
        # Uso de CPU/memória, discos, rede e uptime: o AGENTE vence (mede de
        # dentro do SO, igual ao que o cliente vê). VMware só entra quando o host
        # não tem aquela métrica pelo agente. Capacidade ALOCADA (RAM): VMware
        # vence, por ser o que está reservado/cobrado no cluster.
        for hid, h_it in host_items.items():
            h_it['fonte'] = {}
            for k in ('cpu', 'mem'):
                agente = [i for i in h_it[k] if not _eh_vmware(i)]
                vmw = [i for i in h_it[k] if _eh_vmware(i)]
                if agente:
                    h_it[k] = agente
                    h_it['fonte'][k] = 'agente'
                elif vmw:
                    # CPU: consumo em Hz (vmware.vm.cpu.usage), pedido do cliente;
                    # memória: memória do GUEST (vmware.vm.memory.size.usage.guest),
                    # mais próxima do SO que o % do host (vmware.vm.memory.usage).
                    pref = 'vmware.vm.cpu.usage[' if k == 'cpu' else 'vmware.vm.memory.size.usage.guest['
                    escolhido = [i for i in vmw if (i.get('key_') or '').lower().startswith(pref)]
                    h_it[k] = escolhido or vmw
                    h_it['fonte'][k] = 'vmware'
            if not h_it['_disk_map'] and h_it['_disk_map_vmw']:
                h_it['_disk_map'] = h_it['_disk_map_vmw']
                h_it['fonte']['disk'] = 'vmware'
            elif h_it['_disk_map']:
                h_it['fonte']['disk'] = 'agente'
            if not h_it['nets'] and h_it['nets_vmw']:
                h_it['nets'] = h_it['nets_vmw']
                h_it['fonte']['net'] = 'vmware'
            if h_it['mem_total_vmw'] is not None:
                h_it['mem_total'] = h_it['mem_total_vmw']
            if h_it['uptime'] is None:
                h_it['uptime'] = h_it['uptime_vmw']

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
        # Consolidado do ambiente para o resumo global e o placar de economia.
        economia_vms, vms_cpu_pressao, vms_mem_pressao, vms_pico_isolado, discos_criticos = [], [], [], [], []
        discos_enchendo, vms_reboot, vms_desligadas = [], [], []
        insights_por_vm = []
        
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
            n_vcpu = None            # nº de vCPUs (numérico) para o cálculo de capacidade
            capacidade_ghz = None    # nº_vCPUs × clock_ghz
            if not is_fgt:
                cpu_tot_val, mem_tot_val = "Não Monitorado", "Não Monitorado"
                # vCPUs: usa a alocação ATUAL (lastvalue). O item roda de hora em
                # hora (delay 1h), então em períodos curtos pode não haver
                # histórico — e, se a VM foi redimensionada no período, o que
                # importa para o FinOps é o tamanho de hoje. Sem lastvalue,
                # cai no ponto mais recente do histórico/trend.
                if h_it['cpu_total']:
                    n = None
                    try:
                        lv = h_it['cpu_total'].get('lastvalue')
                        if lv not in (None, ''):
                            n = float(lv)
                    except (TypeError, ValueError):
                        n = None
                    if not n or n <= 0:
                        serie = hist_by_item.get(h_it['cpu_total']['itemid']) or []
                        if serie:
                            try:
                                n = float(serie[-1].get('value_avg', serie[-1].get('value', 0)))
                            except (TypeError, ValueError):
                                n = None
                    if n and 0 < n < 4096:
                        n_vcpu = int(round(n))
                        capacidade_ghz = n_vcpu * clock_ghz
                        cpu_tot_val = f"{n_vcpu} vCPUs"

                if h_it['mem_total']:
                    # RAM alocada ATUAL (lastvalue), como nas vCPUs; histórico só
                    # se não houver lastvalue.
                    v = []
                    try:
                        lv = float(h_it['mem_total'].get('lastvalue') or 0)
                        if lv > 0: v = [lv]
                    except (TypeError, ValueError):
                        pass
                    if not v:
                        v = [float(x.get('value_avg', x.get('value', 0))) for x in hist_by_item.get(h_it['mem_total']['itemid'], [])]
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
            # Discos em GRADE (várias linhas) em vez de uma única linha: com muitos
            # discos, uma linha só espremia os donuts e sobrepunha os nomes.
            disk_cols = min(n_disks, DISCOS_POR_LINHA) if n_disks > 0 else 0
            disk_rows = math.ceil(n_disks / DISCOS_POR_LINHA) if n_disks > 0 else 0
            total_rows = max(1, line_rows + disk_rows)

            fig = Figure(figsize=(14, 4.5 * total_rows), facecolor='#1e1e1e')
            if total_rows > 0:
                gs = gridspec.GridSpec(total_rows, 1, figure=fig, hspace=0.5)

            c_str, m_str, ni_str, no_str, vpn_str, d_str_resumo, u_str = "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A"
            # Capturados no laço de gráficos para a análise FinOps de capacidade
            # (feita após o laço, quando já temos a capacidade em GHz).
            cpu_avg_val = cpu_peak_val = None; cpu_unit = ""
            mem_avg_val = mem_peak_val = None; mem_unit = ""
            cpu_p95_val = cpu_pct_alto = mem_p95_val = mem_pct_alto = None
            insights_finops = []

            # Uptime: usa o valor MAIS RECENTE (lastvalue), não uma média sobre o
            # período — uptime é um contador crescente, então a "média" ao longo de
            # 30 dias subestimaria bastante o tempo online atual da VM.
            item_uptime = h_it.get('uptime')
            if item_uptime and item_uptime.get('lastvalue') not in (None, ''):
                try:
                    segundos_uptime = float(item_uptime['lastvalue'])
                    u_str = _formatar_duracao(segundos_uptime) if segundos_uptime > 0 else "Desligada"
                    if 0 < segundos_uptime < REBOOT_RECENTE_S:
                        insights_finops.append(
                            f"🔄 <b>Reinício recente:</b> a VM está no ar há só {_formatar_duracao(segundos_uptime)}. "
                            f"Confira se foi planejado (updates/manutenção) ou uma queda.")
                        vms_reboot.append(name)
                except (TypeError, ValueError):
                    u_str = "N/A"

            # Estado no vCenter (template VMware): 0 = desligada, 2 = suspensa.
            vm_desligada = False
            _pw = h_it.get('power')
            if _pw is not None and str(_pw.get('lastvalue', '')).strip() in ('0', '2'):
                vm_desligada = True
                _estado = 'desligada' if str(_pw.get('lastvalue')).strip() == '0' else 'suspensa'
                insights_finops.append(
                    f"⏻ <b>VM {_estado} no vCenter:</b> continua reservando disco (e RAM, se suspensa). "
                    f"Se não for mais usada, avalie remover ou arquivar.")
                vms_desligadas.append(name)

            _ultima_serie = {'v': []}  # valores da última série extraída (p/ p95)

            def extract_and_plot(it, ax, plot_kw):
                _ultima_serie['v'] = []
                if it and it['itemid'] in hist_by_item:
                    t, v, u = process_series(hist_by_item[it['itemid']], it.get('units', ''), it['name'])
                    if v:
                        _ultima_serie['v'] = v
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
                            if u == '%':
                                c_str = f"{avg:.1f}%"
                            elif u == 'GHz' and capacidade_ghz:
                                # VMware mede em GHz: mostra o % da capacidade
                                # (comparável às VMs com agente) + o absoluto.
                                c_str = f"{avg / capacidade_ghz * 100:.1f}% ({avg:.1f} GHz)"
                            else:
                                c_str = f"{avg:.1f} {u}".strip()
                            cpu_avg_val, cpu_peak_val, cpu_unit = avg, mx, u
                            _v = _ultima_serie['v']
                            cpu_p95_val = _percentil(_v, 95)
                            # % do tempo acima do limiar de pressão (em % da capacidade)
                            if u == '%':
                                _pct = _v
                            elif 'hz' in (u or '').lower() and capacidade_ghz:
                                _pct = [x / capacidade_ghz * 100 for x in _v]
                            else:
                                _pct = []
                            if _pct:
                                cpu_pct_alto = sum(1 for x in _pct if x >= LIMIAR_PRESSAO_CPU) / len(_pct) * 100
                        elif lp['type'] == 'mem':
                            u = '%' if not u and avg <= 100.5 else u
                            set_y_formatter(ax, u)
                            _mt_gb = _parse_gb(mem_tot_val)
                            if u == '%':
                                m_str = f"{avg:.1f}%"
                            elif u == 'GB' and _mt_gb:
                                m_str = f"{avg / _mt_gb * 100:.1f}% ({_fmt_gb(avg)})"
                            else:
                                m_str = f"{avg:.1f} {u}".strip()
                            mem_avg_val, mem_peak_val, mem_unit = avg, mx, u
                            _v = _ultima_serie['v']
                            mem_p95_val = _percentil(_v, 95)
                            _mt = _parse_gb(mem_tot_val)
                            if u == '%':
                                _pct = _v
                            elif u == 'GB' and _mt:
                                _pct = [x / _mt * 100 for x in _v]
                            else:
                                _pct = []
                            if _pct:
                                mem_pct_alto = sum(1 for x in _pct if x >= LIMIAR_PRESSAO_MEM) / len(_pct) * 100
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

            # Análise FinOps de capacidade (right-sizing agressivo), agora que já
            # temos consumo médio/pico + a capacidade em GHz da VM.
            if not is_fgt:
                _ins, _eco, _al = _finops_capacity_insights(
                    n_vcpu, capacidade_ghz, clock_ghz,
                    cpu_avg_val, cpu_peak_val, cpu_unit,
                    mem_avg_val, mem_peak_val, mem_unit, _parse_gb(mem_tot_val),
                    cpu_p95=cpu_p95_val, cpu_pct_alto=cpu_pct_alto,
                    mem_p95=mem_p95_val, mem_pct_alto=mem_pct_alto,
                    familia=classificar_familia_so(so_por_host.get(hid)))
                insights_finops.extend(_ins)
                if _eco['vcpu'] or _eco['ram_gb']:
                    economia_vms.append({'name': name, 'hid': hid, 'vcpu': _eco['vcpu'], 'ram_gb': _eco['ram_gb']})
                if _al['cpu_pressao']: vms_cpu_pressao.append(name)
                if _al['mem_pressao']: vms_mem_pressao.append(name)
                if _al['cpu_pico_isolado'] is not None: vms_pico_isolado.append(name)

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

            def _serie_uso_disco(items_por_tipo):
                """Série (dias, % usado) do disco para a previsão de enchimento.
                Usa pused, 100-pfree ou used/total, nessa ordem."""
                def _rows(kind):
                    it = items_por_tipo.get(kind)
                    return hist_by_item.get(it['itemid']) if it else None
                def _val(r):
                    return float(r.get('value_avg', r.get('value', 0)))
                pontos = None
                if _rows('pused'):
                    pontos = [(int(r['clock']), _val(r)) for r in _rows('pused')]
                elif _rows('pfree'):
                    pontos = [(int(r['clock']), 100.0 - _val(r)) for r in _rows('pfree')]
                elif _rows('used') and _rows('total'):
                    tot = {int(r['clock']): _val(r) for r in _rows('total')}
                    pontos = [(int(r['clock']), _val(r) / tot[int(r['clock'])] * 100.0)
                              for r in _rows('used') if tot.get(int(r['clock']))]
                if not pontos or len(pontos) < MIN_PONTOS_PREVISAO:
                    return None
                t0 = pontos[0][0]
                return [(c - t0) / 86400.0 for c, _ in pontos], [v for _, v in pontos]

            def _dias_para_encher(items_por_tipo):
                """Dias até 100% no ritmo atual (regressão linear), ou None se o
                disco não está crescendo de forma consistente."""
                serie = _serie_uso_disco(items_por_tipo)
                if not serie:
                    return None
                xs, ys = serie
                slope = _regressao_linear(xs, ys)
                # Ponto de partida: média das últimas 24 amostras (~1 dia), para
                # não depender de um único valor.
                atual = sum(ys[-24:]) / len(ys[-24:])
                if slope <= LIMIAR_CRESC_DISCO_DIA or atual >= 100:
                    return None
                return (100.0 - atual) / slope

            disk_summaries = []
            if n_disks > 0 and not is_fgt:
                # Uma subgrade (1 x disk_cols) por linha de discos. Assim os donuts
                # ficam alinhados numa grade e cada um tem largura suficiente para
                # o nome não sobrepor o vizinho.
                gs_disk_por_linha = {
                    r: gs[line_rows + r].subgridspec(1, disk_cols, wspace=0.45)
                    for r in range(disk_rows)
                }
                for i, grupo_disco in enumerate(h_it['disks']):
                    r, c = divmod(i, disk_cols)
                    ax_d = fig.add_subplot(gs_disk_por_linha[r][0, c])
                    ax_d.set_facecolor('#1e1e1e')

                    used_pct = _calcular_uso_disco(grupo_disco['items'])
                    sem_dado = used_pct is None
                    if sem_dado:
                        used_pct = 0.0

                    _rot = _rotulo_mount(grupo_disco['mount'])
                    disk_summaries.append(f"{_rot} {used_pct:.1f}%" if not sem_dado else f"{_rot} sem dado")
                    cor_uso = '#00e676' if not sem_dado else '#555555'
                    ax_d.pie([used_pct, max(0, 100 - used_pct)], labels=['Uso', 'Livre'], colors=[cor_uso, '#333333'], startangle=90, radius=1.2, wedgeprops=dict(width=0.4, edgecolor='#1e1e1e'), textprops={'color': '#aaaaaa', 'fontsize': 10})
                    texto_central = f'{used_pct:.1f}%' if not sem_dado else 's/ dado'
                    ax_d.text(0, 0, texto_central, ha='center', va='center', fontsize=13 if not sem_dado else 10, fontweight='bold', color='#ffffff')

                    # Título = mount técnico (chave do Zabbix), mas ENCURTADO no
                    # rótulo quando for um caminho longo — preservando o final, que
                    # é o que diferencia /.../web1/log de /.../web8/log. O insight
                    # de disco cheio (abaixo) continua usando o mount COMPLETO.
                    n_amigavel = grupo_disco['mount']
                    ax_d.set_title(_titulo_disco_curto(n_amigavel), fontsize=11, fontweight='bold', pad=16, color='#e0e0e0')

                    dias_cheio = None if sem_dado else _dias_para_encher(grupo_disco['items'])
                    prazo_txt = ""
                    if dias_cheio is not None:
                        data_cheio = datetime.fromtimestamp(hora_atual + dias_cheio * 86400).strftime('%d/%m/%Y')
                        prazo_txt = (f" No ritmo atual, enche em <b>~{dias_cheio:.0f} dias</b> ({data_cheio})."
                                     if dias_cheio <= 730 else "")
                    if not sem_dado and used_pct > 85:
                        insights_finops.append(f"🚨 <b>Disco ({n_amigavel}):</b> {used_pct:.0f}% usado — volume crítico (> 85%). "
                                               f"Expansão recomendada.{prazo_txt}")
                        discos_criticos.append((name, n_amigavel, used_pct))
                    elif dias_cheio is not None and dias_cheio <= DIAS_ALERTA_DISCO:
                        insights_finops.append(f"⏳ <b>Disco ({n_amigavel}):</b> {used_pct:.0f}% usado, mas crescendo.{prazo_txt} "
                                               f"Planeje a expansão.")
                    if dias_cheio is not None and dias_cheio <= DIAS_ALERTA_DISCO:
                        discos_enchendo.append((name, n_amigavel, dias_cheio))

            if disk_summaries: d_str_resumo = " | ".join(disk_summaries)
            
            if is_fgt: insights_finops.append("🛡️ <b>Fortigate (Firewall):</b> Monitoramento ativo das interfaces e túneis estabelecidos.")
            elif not insights_finops: insights_finops.append("✅ <b>Geral:</b> Dimensionamento adequado. Ativo operando dentro da normalidade (Health 100%).")
            
            insights_por_vm.append((name, [_texto_puro(i) for i in insights_finops
                                           if 'Dimensionamento adequado' not in i and 'Fortigate' not in i]))
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
                        {f"Capacidade Computacional: <strong style='color:#00e5ff'>{n_vcpu} vCPUs × {clock_ghz:g} = {capacidade_ghz:.1f} GHz</strong><br>" if capacidade_ghz else ""}
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
            # Texto CURTO na tela, completo no tooltip (o uname do Linux é uma
            # linha enorme que alargava a tabela e cortava o relatório).
            _so_label = html_lib.escape(resumir_so(so_txt)) if so_txt else 'SO não informado'
            _so_title = html_lib.escape(so_txt, quote=True) if so_txt else ''
            so_badge_html = (
                f"<span title='{_so_title}' style='display:inline-block; margin-left:12px; padding:3px 12px; "
                f"background:#121212; border:1px solid {_cor_so}; border-radius:14px; "
                f"font-size:13px; font-weight:600; color:{_cor_so}; vertical-align:middle; "
                f"font-family:sans-serif;'>{_ic} {_so_label}</span>"
            )

            # Selo de CAPACIDADE em GHz no cabeçalho (nº_vCPUs × clock do cliente).
            cap_badge_html = ""
            if capacidade_ghz:
                cap_badge_html = (
                    f"<span style='display:inline-block; margin-left:10px; padding:3px 12px; "
                    f"background:#121212; border:1px solid #00e5ff; border-radius:14px; "
                    f"font-size:13px; font-weight:600; color:#00e5ff; vertical-align:middle; "
                    f"font-family:sans-serif;'>⚡ {capacidade_ghz:.1f} GHz</span>"
                )

            secoes_html_vms.append(f"""
            <div id='vm_{hid}' style='background:#1e1e1e; padding:25px; border-radius:8px; margin-bottom:40px; border:1px solid #333; box-shadow: 0 4px 15px rgba(0,0,0,0.5);'>
                <h2 style='color:#ffffff; margin-top:0; border-bottom:1px solid #333; padding-bottom:10px;'>
                    <span class='status-dot {"gray" if vm_desligada else "green"}'></span> {name}{so_badge_html}{cap_badge_html}
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

            tab_so = f"<td class='col-so' title='{_so_title}' style='color:{_cor_so}; font-size:13px;'>{_ic} {_so_label}</td>"
            linhas_tabela_resumo.append(
                f"<tr><td><span class='status-dot {'gray' if vm_desligada else 'green'}' title='{'Desligada' if vm_desligada else 'Ligada'}'></span></td>"
                f"<td><a href='#vm_{hid}' style='color:#00e5ff; font-weight:bold; text-decoration:none; transition: color 0.3s;' onmouseover=\"this.style.color='#ffffff'\" onmouseout=\"this.style.color='#00e5ff'\">{name}</a></td>"
                f"{tab_so}{tab_resumo}</tr>"
            )

        # ---- Resumo global (coerente: só diz "normal" se não houver alerta) ----
        def _lista_vms(nomes, limite=4):
            nomes = [f"<strong>{n}</strong>" for n in nomes]
            if len(nomes) > limite:
                return ", ".join(nomes[:limite]) + f" e mais {len(nomes) - limite}"
            return ", ".join(nomes)

        pontos = []
        if vms_cpu_pressao:
            pontos.append(f"<li>🚨 <b>CPU sob pressão sustentada</b> (p95 ≥ {LIMIAR_PRESSAO_CPU:.0f}%) em "
                          f"{len(vms_cpu_pressao)} VM(s): {_lista_vms(vms_cpu_pressao)}.</li>")
        if vms_mem_pressao:
            pontos.append(f"<li>🚨 <b>Memória saturada</b> (p95 ≥ {LIMIAR_PRESSAO_MEM:.0f}%) em "
                          f"{len(vms_mem_pressao)} VM(s): {_lista_vms(vms_mem_pressao)}.</li>")
        if discos_criticos:
            _d = sorted(discos_criticos, key=lambda x: -x[2])
            _txt = ", ".join(f"<strong>{n}</strong> ({_rotulo_mount(m)} {p:.0f}%)" for n, m, p in _d[:4])
            if len(_d) > 4: _txt += f" e mais {len(_d) - 4}"
            pontos.append(f"<li>🚨 <b>Discos acima de 85%:</b> {len(_d)} volume(s) — {_txt}.</li>")
        _criticos = {(n, m) for n, m, _p in discos_criticos}
        _enchendo = sorted([d for d in discos_enchendo if (d[0], d[1]) not in _criticos], key=lambda x: x[2])
        if _enchendo:
            _txt = ", ".join(f"<strong>{n}</strong> ({_rotulo_mount(m)} ~{dd:.0f} dias)" for n, m, dd in _enchendo[:4])
            if len(_enchendo) > 4: _txt += f" e mais {len(_enchendo) - 4}"
            pontos.append(f"<li>⏳ <b>Discos que enchem em até {DIAS_ALERTA_DISCO} dias</b> no ritmo atual: {_txt}.</li>")
        if vms_desligadas:
            pontos.append(f"<li>ℹ️ <b>VMs desligadas/suspensas no vCenter:</b> {_lista_vms(vms_desligadas)}. "
                          f"Ainda ocupam recurso alocado; avalie remover ou arquivar.</li>")
        if vms_reboot:
            pontos.append(f"<li>ℹ️ <b>Reinício nas últimas 24h:</b> {_lista_vms(vms_reboot)}.</li>")
        if vms_pico_isolado:
            pontos.append(f"<li>ℹ️ <b>Picos isolados de CPU</b> (sem pressão sustentada) em {len(vms_pico_isolado)} VM(s): "
                          f"{_lista_vms(vms_pico_isolado)}. Provável rotina agendada; não exigem aumento de vCPU.</li>")

        tot_vcpu = sum(e['vcpu'] for e in economia_vms)
        tot_ram = sum(e['ram_gb'] for e in economia_vms)
        if pontos:
            n_acao = sum(1 for p in pontos if '🚨' in p or '⏳' in p)
            abertura = (f"{n_acao} ponto(s) pedem ação no período:" if n_acao
                        else "Nenhum recurso sob pressão sustentada no período. Observações:")
            analise_texto = f"{abertura}<ul style='margin:8px 0 0 0; padding-left:20px;'>{''.join(pontos)}</ul>"
        else:
            analise_texto = ("A infraestrutura operou dentro dos limites normais: nenhuma VM com CPU ou memória "
                             "sob pressão sustentada e nenhum disco acima de 85%.")
        if tot_vcpu or tot_ram:
            analise_texto += (f"<div style='margin-top:8px;'>💰 Oportunidade de otimização: devolver "
                              f"<b>{tot_vcpu} vCPU(s)</b> e <b>{_fmt_gb(tot_ram)}</b> de RAM ao cluster "
                              f"em {len(economia_vms)} VM(s) — detalhes no placar abaixo.</div>")

        # ---- Placar de economia (right-sizing) ----
        placar_html = ""
        if economia_vms:
            top = sorted(economia_vms, key=lambda e: (e['vcpu'], e['ram_gb']), reverse=True)[:5]
            linhas_top = "".join(
                f"<tr><td><a href='#vm_{e['hid']}' style='color:#00e5ff; text-decoration:none; font-weight:bold;'>{e['name']}</a></td>"
                f"<td>{('−' + str(e['vcpu'])) if e['vcpu'] else '—'}</td>"
                f"<td>{('−' + _fmt_gb(e['ram_gb'])) if e['ram_gb'] else '—'}</td></tr>"
                for e in top)
            placar_html = f"""
        <div class='placar'>
            <div class='placar-cards'>
                <div class='placar-card'><div class='placar-num'>{tot_vcpu}</div><div class='placar-lbl'>vCPUs devolvíveis</div></div>
                <div class='placar-card'><div class='placar-num'>{_fmt_gb(tot_ram)}</div><div class='placar-lbl'>RAM devolvível</div></div>
                <div class='placar-card'><div class='placar-num'>{len(economia_vms)}</div><div class='placar-lbl'>VMs com right-sizing</div></div>
            </div>
            <h3 style='color:#00e5ff; margin:22px 0 10px 0; font-size:16px;'>Top {len(top)} VMs com mais recurso a devolver</h3>
            <div class='table-wrap' style='margin-bottom:0;'><table class='summary-table' style='margin-bottom:0;'>
                <thead><tr><th>VM</th><th>vCPUs</th><th>RAM</th></tr></thead><tbody>{linhas_top}</tbody>
            </table></div>
            <div style='color:#888; font-size:12px; margin-top:10px;'>Recomendações cobrem o p95 do consumo com {int((FOLGA_RIGHTSIZING-1)*100)}% de folga,
            com mínimo de {MIN_VCPU['Windows']} vCPUs / {_fmt_gb(MIN_RAM_GB['Windows'])} no Windows e corte de no máximo {int(CORTE_MAXIMO*100)}% por rodada.</div>
        </div>"""

        # ---- Resumo executivo escrito pela IA (item 9) ----
        # As REGRAS acima continuam sendo a fonte dos números; a IA só redige e
        # prioriza a partir deles. Se a IA não estiver configurada ou falhar, o
        # relatório sai normalmente só com o resumo por regras.
        resumo_ia_html, rotulo_ia = _resumo_executivo_ia(
            criado_por, grupo, dias, len([h for h in hosts]), pontos, tot_vcpu, tot_ram,
            economia_vms, insights_por_vm)
        if resumo_ia_html:
            bloco_resumo = f"""
        <div class='disclaimer-box'>
            <strong style='color:#00e5ff; font-size:16px;'>🤖 Resumo Executivo da IA</strong>
            <span style='color:#777; font-size:12px;'> · {html_lib.escape(rotulo_ia)}</span><br>
            {resumo_ia_html}
        </div>
        <div class='disclaimer-box' style='border-left-color:#ff9e00;'>
            <strong style='color:#ff9e00; font-size:16px;'>📋 Pontos de atenção (regras automáticas):</strong><br>
            {analise_texto}
        </div>"""
        else:
            bloco_resumo = f"""
        <div class='disclaimer-box'>
            <strong style='color:#00e5ff; font-size:16px;'>🤖 Resumo Global da IA:</strong><br>
            {analise_texto}
        </div>"""

        html_filename = f"Dashboard_Executivo_{int(time.time())}.html"

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
            /* Tabela nunca mais vaza do container: textos longos quebram linha
               e, se ainda assim não couber (tela estreita), rola na horizontal
               DENTRO do bloco em vez de cortar a página. */
            .table-wrap{{width:100%; overflow-x:auto; margin-bottom:40px;}}
            .table-wrap .summary-table{{margin-bottom:0;}}
            .summary-table td{{overflow-wrap:anywhere; word-break:break-word; vertical-align:middle;}}
            .summary-table td.col-so{{max-width:280px;}}
            .summary-table td:nth-child(2){{max-width:300px;}}
            .status-dot{{display:inline-block; width:14px; height:14px; border-radius:50%; margin-right:10px; vertical-align:middle;}}
            .green{{background:#00e676; box-shadow: 0 0 8px #00e676;}}
            .gray{{background:#666; box-shadow:none;}}
            .placar{{background:#141d16; border:1px solid #1f3a26; border-left:4px solid #00e676; border-radius:6px; padding:20px 25px; margin-bottom:40px;}}
            .placar-cards{{display:flex; flex-wrap:wrap; gap:16px;}}
            .placar-card{{flex:1; min-width:160px; background:#121212; border:1px solid #2a2a2a; border-radius:6px; padding:14px 18px;}}
            .placar-num{{font-size:30px; font-weight:700; color:#00e676;}}
            .placar-lbl{{font-size:13px; color:#aaa; margin-top:4px;}}
        </style></head><body><div class='container'>
        {branding.get_logo_html()}
        <h1>Dashboard Executivo & FinOps | {grupo}</h1>
        <div class='report-info'><strong>Período de Análise:</strong> Últimos {dias} dias<br><strong>Gerado em:</strong> {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>

        {bloco_resumo}
        {placar_html}

        <div class='table-wrap'><table class='summary-table'><thead><tr><th>Status</th><th>Máquina Virtual (Clique para Detalhes)</th><th>Sistema Operacional</th><th>Métricas Médias no Período</th></tr></thead>
        <tbody>{"".join(linhas_tabela_resumo)}</tbody></table></div>

        <h2 style='color:#00e5ff; margin-top:50px; margin-bottom:20px; text-align:center; font-size: 28px;'>Análise de Capacity (AI) e Gráficos por Servidor</h2>
        {"".join(secoes_html_vms)}
        </div></body></html>"""

        # Armazena em disco PRIVADO (instance/reports/) + metadados no banco, e
        # gera um id inadivinhável. O download é por rota AUTENTICADA (/reports/<id>)
        # com checagem de tenant — não mais em /static, que era público.
        report_id = report_store.salvar_relatorio(
            full_html,
            grupo=grupo,
            criado_por=criado_por,
            titulo=f"FinOps | {grupo} | {dias} dia(s)"
        )

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

        if report_id:
            botao_html = (
                f"<a href='/reports/{report_id}' target='_blank' rel='noopener' "
                f"download='{html_filename}' "
                "style='display: inline-block; background-color: #00e5ff; color: #121212; padding: 10px 20px; "
                "text-decoration: none; border-radius: 5px; font-family: sans-serif; font-weight: bold; font-size: 13px; "
                "border: 1px solid #00b4d8; margin-top: 5px; transition: background-color 0.3s;' "
                "onmouseover=\"this.style.backgroundColor='#00b4d8'\" onmouseout=\"this.style.backgroundColor='#00e5ff'\">"
                "📥 Baixar Relatório</a>"
            )
        else:
            # Falha ao armazenar: não deixa o cliente sem resposta nem cria link quebrado.
            botao_html = (
                "<p style='color:#ffb020; font-size:13px; font-family:sans-serif; margin-top:5px;'>"
                "⚠️ O relatório foi gerado, mas houve uma falha ao armazená-lo. "
                "Tente gerar novamente.</p>"
            )

        return html_progress + msg_chat + botao_html
    except Exception as e: return f"Erro ao gerar relatório global: {str(e)}"

def gerar_grafico_zabbix(nome_host: str, metrica: str, quantidade_dias: float, token: str, grupo: str = None, url: str = None, criado_por: str = None, clock_vcpu: float = None) -> str:
    """Relatório de UMA VM específica, pedida por nome (ex.: "gere relatório
    da VM CLIENTE_ASV_VM_ASV-BD01-IPEL"). Reaproveita o MESMO motor/layout do
    relatório de "TODOS" (gerar_relatorio_consumo_todos), só restringindo a
    lista de hosts a quem bate com o nome pedido — por isso o relatório sai
    completo (CPU, memória, rede, discos), não só a 'metrica' isolada.

    IMPORTANTE (multi-tenant): 'grupo' é sempre respeitado — a busca pelo nome
    do host NUNCA sai do grupo/conta do usuário que pediu o relatório, mesmo
    que exista uma VM com nome parecido em outra conta.
    """
    return gerar_relatorio_consumo_todos(quantidade_dias, token, grupo, url, host_filter=nome_host, criado_por=criado_por, clock_vcpu=clock_vcpu)




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


def montar_inventario_para_ia(token, grupo, url=None, clock_vcpu=None):
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
        try:
            _clock = float(clock_vcpu) if clock_vcpu and float(clock_vcpu) > 0 else GHZ_POR_VCPU
        except (TypeError, ValueError):
            _clock = GHZ_POR_VCPU
        filtro = {}
        if grupo and grupo != "GLOBAL":
            g_res = zabbix_api("hostgroup.get", {"search": {"name": grupo}}, token, url)
            if g_res:
                filtro["groupids"] = g_res[0].get('groupid')

        hosts = zabbix_api("host.get", {"output": ["hostid", "name"], **filtro}, token, url)
        if not hosts:
            return ""
        # Ignora hosts agregadores de mapa (Alertas / Soma de tráfego) — não são
        # VMs reais e só confundiriam a contagem/análise da IA.
        hosts = [h for h in hosts if not _eh_host_agregador(h.get('name', ''))]
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

        dados = {hid: {
            'vcpu': None,
            'ram_bytes': None, 'ram': None,
            # --- VMware API (fonte primária, casada por CHAVE exata) ---
            'cpu_usage_ghz': None,     # vmware.vm.cpu.usage (consumo)  -> numerador
            'cpu_capacity_ghz': None,  # vmware.vm.cpu.capacity.ghz     -> denominador
            'cpu_pct_perf': None,      # vmware.vm.cpu.usage.perf (% pronto, fallback)
            'mem_guest_bytes': None,   # vmware.vm.memory.size.usage.guest (bytes)
            'mem_host_pct': None,      # vmware.vm.memory.usage (% do HOST, diverge do SO)
            # --- Agente Zabbix / templates não-VMware (fallback) ---
            'cpu_pct_agent': None,
            'mem_pct_agent': None,
            'disks': {},
        } for hid in host_ids}

        def _num(lv):
            try:
                return float(lv)
            except (TypeError, ValueError):
                return None

        def _melhor_uso(atual, novo_val):
            # Mantém o MAIOR uso quando há vários itens do mesmo tipo (pior caso).
            if novo_val is None:
                return atual
            if atual is None or novo_val > atual:
                return novo_val
            return atual

        def _freq_para_ghz(units, val):
            """Normaliza um valor de FREQUÊNCIA para GHz. Aceita Hz/MHz/GHz pela
            unidade e, na ausência dela, decide pela ordem de grandeza. Cobre o
            item vmware.vm.cpu.usage (vem em Hz: 1.953.000.000 = 1,953 GHz) e o
            calculado vmware.vm.cpu.capacity.ghz (já em GHz: 24)."""
            if val is None:
                return None
            u = (units or '').lower()
            if 'ghz' in u:
                return val
            if 'mhz' in u:
                return val / 1e3
            if 'hz' in u:
                return val / 1e9
            if val >= 1e6:
                return val / 1e9     # Hz
            if val >= 1e4:
                return val / 1e3     # MHz
            return val               # já em GHz (ex.: 24)

        # Heurísticas de NOME/UNIDADE — usadas só no FALLBACK (agente/não-VMware).
        _USO_REGEX = re.compile(r'(uso|usage|utiliz|util|percent|carga|load|ocupa)', re.IGNORECASE)
        _CORE_REGEX = re.compile(r'(núcleo|nucleo|core|cores|number|númer|numer|\bnum\b|count|quantidade|vcpu)', re.IGNORECASE)

        for it in items:
            hid = it.get('hostid')
            if hid not in dados:
                continue
            nl = (it.get('name') or '').lower()
            ul = (it.get('units') or '').lower()
            val = _num(it.get('lastvalue'))
            # Chave técnica sem os parâmetros [{$VMWARE.URL},...]: é o que
            # identifica o item de forma inequívoca, em qualquer idioma.
            key_base = (it.get('key_') or '').split('[', 1)[0].strip().lower()

            # ================= CORRESPONDÊNCIA POR CHAVE EXATA (VMware API) ==========
            # É o que elimina de vez a confusão que dava "100% em todas": o item de
            # CONSUMO (cpu.usage) e o de CAPACIDADE (cpu.capacity.ghz) agora são
            # lidos em campos separados, nunca um no lugar do outro.
            # vCPUs pela chave: vmware.vm.cpu.num, system.cpu.num (agente Linux)
            # e wmi NumberOfLogicalProcessors (agente Windows).
            if eh_item_vcpu(it) == 'key':
                if val is not None and 0 < val < 4096:
                    dados[hid]['vcpu'] = int(round(val))
                continue
            if key_base == 'vmware.vm.cpu.usage':               # consumo (Hz -> GHz)
                g = _freq_para_ghz(ul, val)
                if g is not None:
                    dados[hid]['cpu_usage_ghz'] = _melhor_uso(dados[hid]['cpu_usage_ghz'], g)
                continue
            if key_base == 'vmware.vm.cpu.capacity.ghz':        # capacidade (GHz)
                c = _freq_para_ghz(ul, val)
                if c and c > 0:
                    dados[hid]['cpu_capacity_ghz'] = c
                continue
            if key_base == 'vmware.vm.cpu.usage.perf':          # % pronto (fallback)
                if val is not None:
                    dados[hid]['cpu_pct_perf'] = _melhor_uso(dados[hid]['cpu_pct_perf'], val)
                continue
            if key_base == 'vmware.vm.memory.size':             # RAM total (bytes)
                if val is not None and val > 0:
                    dados[hid]['ram_bytes'] = val
                    dados[hid]['ram'] = _fmt_ram_bytes(val)
                continue
            if key_base == 'vmware.vm.memory.size.usage.guest':  # guest usado (bytes)
                if val is not None and val >= 0:
                    dados[hid]['mem_guest_bytes'] = val
                continue
            if key_base == 'vmware.vm.memory.usage':            # % do HOST (fallback)
                if val is not None:
                    dados[hid]['mem_host_pct'] = val
                continue

            # Disco (VMware ou agente): já tratado pela chave técnica.
            mount, kind = _parse_disk_key(it.get('key_', ''))
            if mount and kind in ('pused', 'pfree'):
                ident = _norm_mount(mount)
                if ident and not DISCOS_BLOQUEADOS_REGEX.search(mount) and val is not None:
                    usado = val if kind == 'pused' else max(0.0, 100.0 - val)
                    ant = dados[hid]['disks'].get(ident)
                    if ant is None or usado > ant:
                        dados[hid]['disks'][ident] = usado
                continue

            # ================= FALLBACK POR NOME (agente Zabbix / não-VMware) ========
            # Só preenche o que a API não trouxe. Nunca sobrescreve dado da API.
            eh_cpu = 'cpu' in nl or 'processad' in nl
            eh_mem = bool(re.search(r'\b(mem|memória|memoria|memory|ram)\b', nl))
            eh_core = bool(_CORE_REGEX.search(nl))
            eh_percentual = ('%' in ul) or bool(_USO_REGEX.search(nl))
            eh_freq = 'hz' in ul

            if eh_cpu and eh_core and not eh_freq and '%' not in ul:
                if dados[hid]['vcpu'] is None and val is not None and 0 < val < 4096:
                    dados[hid]['vcpu'] = int(val)
            elif eh_cpu and not eh_core and eh_percentual and not eh_freq:
                if val is not None and val <= 100:
                    dados[hid]['cpu_pct_agent'] = _melhor_uso(dados[hid]['cpu_pct_agent'], val)
            elif eh_mem and 'total' in nl and 'uso' not in nl and not eh_percentual:
                if dados[hid]['ram_bytes'] is None and val is not None and val > 1000:
                    dados[hid]['ram_bytes'] = val
                    dados[hid]['ram'] = _fmt_ram_bytes(val)
            elif eh_mem and eh_percentual and 'total' not in nl:
                if val is not None and val <= 100:
                    dados[hid]['mem_pct_agent'] = _melhor_uso(dados[hid]['mem_pct_agent'], val)

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

            # ---- CPU: consumo (cpu.usage) ÷ capacidade (capacity.ghz) ----
            # É o par que, via API VMware (sem agente no guest), mais se aproxima
            # do que o SO mostra. Ordem de preferência do denominador:
            #   1) capacity.ghz (real, calculado pelo Zabbix)
            #   2) vCPUs × 2 GHz (padrão do cluster) — se não houver capacity.ghz
            us_ghz = d['cpu_usage_ghz']
            cap = d['cpu_capacity_ghz'] or ((d['vcpu'] * _clock) if d['vcpu'] else None)
            if us_ghz is not None and cap:
                pct = min(100.0, max(0.0, us_ghz / cap * 100.0))
                cpu = f"{pct:.0f}% ({us_ghz:.2f} de {cap:.1f} GHz)"
            elif d['cpu_pct_perf'] is not None:
                cpu = f"{d['cpu_pct_perf']:.0f}%"
            elif d['cpu_pct_agent'] is not None:
                cpu = f"{d['cpu_pct_agent']:.0f}%"
            elif us_ghz is not None:
                cpu = f"{us_ghz:.2f} GHz (capacidade n/d)"
            else:
                cpu = "n/d"

            # ---- Memória: guest usado (bytes) ÷ RAM total (bytes) ----
            # vmware.vm.memory.size.usage.guest é a memória REAL do guest; o item
            # em % (vmware.vm.memory.usage) é do HOST e diverge do SO, por isso só
            # entra como último recurso.
            if d['mem_guest_bytes'] is not None and d['ram_bytes']:
                pct = min(100.0, max(0.0, d['mem_guest_bytes'] / d['ram_bytes'] * 100.0))
                mem = f"{pct:.0f}%"
            elif d['mem_pct_agent'] is not None:
                mem = f"{d['mem_pct_agent']:.0f}%"
            elif d['mem_host_pct'] is not None:
                mem = f"{d['mem_host_pct']:.0f}% (host)"
            else:
                mem = "n/d"

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
            "Base de cálculo (via API VMware): 'CPU agora' = consumo (vmware.vm.cpu.usage) ÷ "
            "capacidade (vmware.vm.cpu.capacity.ghz) × 100. 'Memória agora' = memória do guest "
            "(vmware.vm.memory.size.usage.guest) ÷ RAM total × 100 — os itens que mais se "
            "aproximam do que o Sistema Operacional reporta.\n"
            "('n/d' = a métrica não é monitorada para essa VM no Zabbix)\n\n"
            "| VM | Sistema Operacional | vCPUs | RAM | CPU agora | Memória agora | Disco |\n"
            "|----|--------------------|-------|-----|-----------|---------------|-------|\n"
        )
        return cabecalho + "\n".join(linhas) + "\n"

    except Exception as e:
        # Enriquecimento opcional: se falhar, o chat segue com o contexto básico.
        print(f"[zabbix] Aviso: falha ao montar inventário para a IA: {e}")
        return ""
