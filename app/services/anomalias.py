"""
DETECÇÃO DE ANOMALIAS (z-score) — Oráculo AI
============================================
Compara o comportamento da ÚLTIMA HORA de cada métrica contra o baseline dos
últimos N dias (média ± desvio padrão dos trends horários) e reporta desvios
em sigmas — tanto PICOS anormais quanto QUEDAS anormais (ex.: tráfego que
despencou = possível serviço parado, mesmo sem nenhum trigger disparado).

Inspirado no workflow /zabbix-anomaly do initMAX/zabbix-ai-skills, implementado
nativamente sobre o motor do Oráculo: usa o fetch_historical_data adaptativo
(à prova de timeout) e o escopo multi-tenant por grupo do cliente.
"""
import re
import time
import statistics
from datetime import datetime

from app.services.zabbix import (
    fetch_historical_data,
    _coletar_hosts_e_itens,
    _parse_disk_key,
)

# Piso de desvio padrão (floor) por categoria, na unidade NATIVA (crua, como
# vem do Zabbix), para evitar falso positivo em métricas quase constantes —
# ex.: um link ocioso que oscila entre 2 e 5 Kbps tem desvio minúsculo, e sem
# o piso QUALQUER variação normal viraria "anomalia de 20 sigmas".
# Para métricas de unidade grande (Hz, bytes), o piso relativo de 5% da média
# (aplicado no cálculo) é quem domina — o piso absoluto aqui é só a rede de
# segurança para métricas em escala pequena.
# >> CALIBRAGEM: se aparecerem falsos positivos no dia a dia (ex.: backups
# noturnos legítimos), aumente o piso da categoria ou o LIMIAR_ATENCAO.
_ANOM_FLOORS = {'cpu': 1.0, 'mem': 1.0, 'disk': 0.5, 'net': 250_000.0, 'io': 500_000.0, 'vpn': 1.0}
_ANOM_LABELS = {'cpu': 'CPU', 'mem': 'Memória', 'disk': 'Disco', 'net': 'Rede', 'io': 'Disco I/O', 'vpn': 'VPN'}

LIMIAR_ATENCAO = 3.0   # desvios (σ) para marcar ATENÇÃO
LIMIAR_CRITICO = 5.0   # desvios (σ) para marcar CRÍTICO
MIN_PONTOS_BASELINE = 24  # exige ao menos ~1 dia de trends para confiar no baseline

# Nomes de item que indicam I/O de DISCO (leitura/escrita), e não tráfego de
# rede — sem isso, "Média de bytes escritos do disco" era rotulado [Rede].
_IO_DISCO_REGEX = re.compile(r'\b(disco|disk|iops|leitura|escrita|lidos?|escritos?|gravad\w*|read|write|written)\b', re.IGNORECASE)


def _classificar_item_anomalia(it):
    """Retorna a categoria do item para análise de anomalia, ou None se o item
    não for de uma das métricas monitoradas.

    A classificação tenta primeiro pela CHAVE técnica (robusta, igual em
    qualquer idioma de template) e só depois pelas heurísticas de nome.
    Itens VMware fixados por pedido do cliente: 'Uso de CPU em MHz'
    (vmware.vm.cpu.usage) e 'Uso de memória da VM'
    (vmware.vm.memory.size.usage.guest) SEMPRE entram na análise."""
    nl = it['name'].lower()
    kl = (it.get('key_') or '').lower()

    # --- por CHAVE técnica (prioridade) ---
    if 'vmware.vm.cpu.usage' in kl:          # cobre .usage (Hz/MHz) e .usage.perf (%)
        return 'cpu'
    if 'vmware.vm.memory.usage' in kl or 'vmware.vm.memory.size.usage' in kl:
        return 'mem'
    mount, kind = _parse_disk_key(it.get('key_', ''))
    if mount and kind in ('pused', 'pfree'):
        return 'disk'

    # --- por NOME (heurísticas já validadas no relatório FinOps) ---
    if 'cpu' in nl and re.search(r'\b(uso|usage|util|percent|mhz|ghz)\b', nl):
        return 'cpu'
    if re.search(r'\b(mem|memória|memory)\b', nl) and re.search(r'\b(uso|usage|util|percent)\b', nl):
        return 'mem'
    if re.search(r'\b(ssl|vpn|ipsec|tunnel|túnel)\b', nl) and re.search(r'\b(user|usuário|active|ativos)\b', nl):
        return 'vpn'
    if re.search(r'\b(bits?|bytes?|traffic|tráfego|trafego|bps|octets?|iops)\b', nl):
        # bytes/bits DE DISCO = I/O; caso contrário, tráfego de rede
        if _IO_DISCO_REGEX.search(nl):
            return 'io'
        if not re.search(r'\b(loopback|lo|veth|docker|speed|velocidade|drop|error|discard|status|estado)\b', nl):
            return 'net'
    return None


def _fmt_hz(v):
    """Formata frequência (valor em Hz)."""
    if v >= 1e9: return f"{v/1e9:.2f} GHz"
    if v >= 1e6: return f"{v/1e6:.0f} MHz"
    if v >= 1e3: return f"{v/1e3:.0f} KHz"
    return f"{v:.0f} Hz"


def _fmt_bytes(v, sufixo=''):
    """Formata tamanho (valor em bytes). sufixo='/s' para taxas de I/O."""
    for lim, unid in ((1024**4, 'TB'), (1024**3, 'GB'), (1024**2, 'MB'), (1024, 'KB')):
        if v >= lim: return f"{v/lim:.1f} {unid}{sufixo}"
    return f"{v:.0f} B{sufixo}"


def _fmt_metrica(valor, categoria, units=''):
    """Formata um valor cru na unidade humana REAL do item (só para EXIBIÇÃO;
    toda a matemática de z-score acontece nos valores crus). A regra de ouro:
    a unidade do Zabbix manda — '%' só aparece se o item FOR percentual.
    Antes, todo item de CPU/memória era exibido como '%', o que gerava
    aberrações tipo '45200000.0%' para 'Uso de CPU em MHz' (valor em Hz)."""
    u = (units or '').strip()
    ul = u.lower()

    if '%' in u:
        return f"{valor:.1f}%"
    if categoria == 'vpn':
        return f"{int(round(valor))} usuários"
    if categoria == 'io':
        # I/O de disco: convenção de mercado é BYTES por segundo (MB/s).
        # ATENÇÃO case-sensitive: 'bps' (minúsculo) = bits/s → converte;
        # 'Bps'/'B'/bytes = já está em bytes/s → usa direto.
        if u == 'bps':
            return _fmt_bytes(valor / 8, '/s')
        return _fmt_bytes(valor, '/s')
    if categoria == 'net':
        # Rede: convenção é BITS por segundo (Mbps). 'Bps'/bytes converte p/ bits.
        eh_bytes = u == 'Bps' or u == 'B' or 'byte' in ul
        bits = valor * 8 if eh_bytes else valor
        mbps = bits / 1e6
        if mbps >= 1000: return f"{mbps/1000:.2f} Gbps"
        if mbps >= 1:    return f"{mbps:.1f} Mbps"
        return f"{bits/1e3:.0f} Kbps"
    if categoria == 'cpu':
        if ul == 'hz' or not u:      # vmware.vm.cpu.usage devolve Hz
            return _fmt_hz(valor)
        if ul == 'mhz':
            return _fmt_hz(valor * 1e6)
        if ul == 'ghz':
            return _fmt_hz(valor * 1e9)
    if categoria == 'mem' and (u == 'B' or 'byte' in ul or not u):
        return _fmt_bytes(valor)     # vmware.vm.memory.size.usage.guest devolve bytes
    if categoria == 'disk':
        return f"{valor:.1f}%"
    return f"{valor:.2f} {u}".strip()


def detectar_anomalias_zabbix(dias_baseline: float, token: str, grupo: str, url: str = None) -> str:
    dias = max(2, min(int(float(dias_baseline)), 30))
    agora = int(time.time())
    inicio = agora - dias * 24 * 3600

    try:
        hosts, items = _coletar_hosts_e_itens(token, grupo, url)
        if not hosts: return "Nenhum servidor encontrado no grupo."
        nome_host = {h['hostid']: h['name'] for h in hosts}

        alvos = []
        for it in items:
            cat = _classificar_item_anomalia(it)
            if cat:
                it['_cat'] = cat
                alvos.append(it)
        if not alvos: return "Não encontrei métricas elegíveis (CPU/memória/disco/rede/VPN) para analisar."

        # dias >= 2 garante trend.get (granularidade horária) no fetch adaptativo
        hist = fetch_historical_data(alvos, inicio, agora, dias, token, url)

        anomalias = []
        analisadas = 0
        for it in alvos:
            rows = hist.get(it['itemid'])
            if not rows or len(rows) < MIN_PONTOS_BASELINE + 1:
                continue
            # rows já vem ordenado por clock (fetch adaptativo ordena no final).
            # Último ponto = "agora" (média da última hora); só vale se for fresco.
            ultimo = rows[-1]
            if agora - int(ultimo['clock']) > 3 * 3600:
                continue  # item sem coleta recente: não dá pra afirmar nada do presente
            atual = float(ultimo.get('value_avg', ultimo.get('value', 0)))
            base = [float(r.get('value_avg', r.get('value', 0))) for r in rows[:-1]]
            analisadas += 1

            media = statistics.fmean(base)
            desvio = statistics.pstdev(base)
            piso = _ANOM_FLOORS.get(it['_cat'], 1.0)
            desvio_efetivo = max(desvio, abs(media) * 0.05, piso)
            z = (atual - media) / desvio_efetivo
            if abs(z) < LIMIAR_ATENCAO:
                continue

            anomalias.append({
                'host': nome_host.get(it['hostid'], '?'),
                'metrica': it['name'],
                'cat': it['_cat'],
                'atual': atual, 'media': media, 'desvio': desvio,
                'z': z, 'units': it.get('units', '')
            })

        cabecalho = (
            f"<div style='background:#1e1e1e; padding:20px; border-radius:8px; border:1px solid #333; font-family:sans-serif;'>"
            f"<h3 style='color:#00e5ff; margin:0 0 5px 0;'>🔍 Detecção de Anomalias (Oráculo AI)</h3>"
            f"<div style='color:#888; font-size:13px; margin-bottom:15px;'>Baseline: últimos {dias} dias (trends horários) · "
            f"{len(hosts)} hosts · {analisadas} métricas analisadas · Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>"
        )

        if not anomalias:
            return (cabecalho +
                    "<div style='background:#12261a; border-left:4px solid #00e676; padding:15px; border-radius:6px; color:#ccc;'>"
                    "✅ <strong style='color:#00e676;'>Nenhum comportamento anômalo detectado.</strong><br>"
                    "Todas as métricas da última hora estão dentro do padrão histórico do ambiente "
                    f"(desvio inferior a {LIMIAR_ATENCAO:.0f}σ do baseline).</div></div>")

        anomalias.sort(key=lambda a: abs(a['z']), reverse=True)
        TETO_LINHAS = 25
        exibidas, ocultas = anomalias[:TETO_LINHAS], max(0, len(anomalias) - TETO_LINHAS)

        linhas = []
        for a in exibidas:
            critico = abs(a['z']) >= LIMIAR_CRITICO
            cor = '#ff4d4d' if critico else '#ff9e00'
            selo = 'CRÍTICO' if critico else 'ATENÇÃO'
            seta = '▲ Pico' if a['z'] > 0 else '▼ Queda'
            linhas.append(
                f"<tr>"
                f"<td style='padding:10px; border-bottom:1px solid #333;'><span style='background:{cor}22; color:{cor}; "
                f"padding:2px 8px; border-radius:4px; font-size:11px; font-weight:bold;'>{selo}</span></td>"
                f"<td style='padding:10px; border-bottom:1px solid #333; color:#fff; font-weight:bold;'>{a['host']}</td>"
                f"<td style='padding:10px; border-bottom:1px solid #333; color:#ccc;'>"
                f"<span style='color:#888; font-size:11px;'>[{_ANOM_LABELS[a['cat']]}]</span> {a['metrica']}</td>"
                f"<td style='padding:10px; border-bottom:1px solid #333; color:{cor}; font-family:monospace; font-weight:bold;'>"
                f"{_fmt_metrica(a['atual'], a['cat'], a['units'])}</td>"
                f"<td style='padding:10px; border-bottom:1px solid #333; color:#888; font-family:monospace;'>"
                f"{_fmt_metrica(a['media'], a['cat'], a['units'])} ± {_fmt_metrica(a['desvio'], a['cat'], a['units'])}</td>"
                f"<td style='padding:10px; border-bottom:1px solid #333; color:{cor}; font-family:monospace; font-weight:bold;'>"
                f"{seta} {abs(a['z']):.1f}σ</td>"
                f"</tr>"
            )

        n_crit = sum(1 for a in anomalias if abs(a['z']) >= LIMIAR_CRITICO)
        resumo = (f"<div style='background:#2b1616; border-left:4px solid #ff4d4d; padding:12px 15px; border-radius:6px; "
                  f"color:#ccc; margin-bottom:15px;'>⚠️ <strong style='color:#ff4d4d;'>{len(anomalias)} anomalia(s) detectada(s)</strong>"
                  f" — {n_crit} crítica(s) (≥{LIMIAR_CRITICO:.0f}σ) e {len(anomalias)-n_crit} de atenção (≥{LIMIAR_ATENCAO:.0f}σ). "
                  f"Comportamento da última hora fora do padrão histórico. "
                  f"<span style='color:#888;'>Anomalia ≠ problema: significa comportamento incomum que merece um olhar "
                  f"(pode ser um backup, uma transferência legítima ou um incidente real começando).</span></div>")

        tabela = ("<table style='width:100%; border-collapse:collapse; font-size:13px;'>"
                  "<thead><tr style='background:#242424;'>"
                  "<th style='padding:10px; text-align:left; color:#00e5ff; font-size:11px;'>NÍVEL</th>"
                  "<th style='padding:10px; text-align:left; color:#00e5ff; font-size:11px;'>HOST</th>"
                  "<th style='padding:10px; text-align:left; color:#00e5ff; font-size:11px;'>MÉTRICA</th>"
                  "<th style='padding:10px; text-align:left; color:#00e5ff; font-size:11px;'>ÚLTIMA HORA</th>"
                  f"<th style='padding:10px; text-align:left; color:#00e5ff; font-size:11px;'>BASELINE ({dias}D)</th>"
                  "<th style='padding:10px; text-align:left; color:#00e5ff; font-size:11px;'>DESVIO</th>"
                  "</tr></thead><tbody>" + "".join(linhas) + "</tbody></table>")

        rodape_partes = []
        if ocultas:
            rodape_partes.append(f"+ {ocultas} anomalia(s) de menor desvio omitida(s).")
        rodape_partes.append("<strong>σ (sigma)</strong> = desvio padrão: mede o quão longe o valor atual está "
                             "da média histórica. Até 2σ é variação normal; 3σ é raro (~0,3% do tempo); "
                             "5σ+ é praticamente impossível de acontecer por acaso — algo mudou de verdade no ambiente.")
        rodape = f"<div style='color:#666; font-size:12px; margin-top:10px;'>{' '.join(rodape_partes)}</div>"

        return cabecalho + resumo + tabela + rodape + "</div>"

    except Exception as e:
        return f"Erro na detecção de anomalias: {str(e)}"

