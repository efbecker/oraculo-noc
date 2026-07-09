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
# >> CALIBRAGEM: se aparecerem falsos positivos no dia a dia (ex.: backups
# noturnos legítimos), aumente o piso da categoria ou o LIMIAR_ATENCAO.
_ANOM_FLOORS = {'cpu': 1.0, 'mem': 1.0, 'disk': 0.5, 'net': 250_000.0, 'vpn': 1.0}
_ANOM_LABELS = {'cpu': 'CPU', 'mem': 'Memória', 'disk': 'Disco', 'net': 'Rede', 'vpn': 'VPN'}

LIMIAR_ATENCAO = 3.0   # desvios (σ) para marcar ATENÇÃO
LIMIAR_CRITICO = 5.0   # desvios (σ) para marcar CRÍTICO
MIN_PONTOS_BASELINE = 24  # exige ao menos ~1 dia de trends para confiar no baseline


def _classificar_item_anomalia(it):
    """Retorna a categoria do item para análise de anomalia, ou None se o item
    não for de uma das métricas monitoradas. Reaproveita as mesmas heurísticas
    de nome/chave já validadas no relatório FinOps."""
    nl = it['name'].lower()
    mount, kind = _parse_disk_key(it.get('key_', ''))
    if mount and kind in ('pused', 'pfree'):
        return 'disk'
    if 'cpu' in nl and re.search(r'\b(uso|usage|util|percent)\b', nl):
        return 'cpu'
    if re.search(r'\b(mem|memória|memory)\b', nl) and re.search(r'\b(uso|usage|util|percent)\b', nl):
        return 'mem'
    if re.search(r'\b(ssl|vpn|ipsec|tunnel|túnel)\b', nl) and re.search(r'\b(user|usuário|active|ativos)\b', nl):
        return 'vpn'
    if (not re.search(r'\b(loopback|lo|veth|docker|speed|velocidade|drop|error|discard|status|estado)\b', nl)
            and re.search(r'\b(bits?|bytes?|traffic|tráfego|trafego|bps|octets?)\b', nl)):
        return 'net'
    return None


def _fmt_metrica(valor, categoria, units=''):
    """Formata um valor cru na unidade humana da categoria (só para EXIBIÇÃO;
    toda a matemática de z-score acontece nos valores crus)."""
    if categoria in ('cpu', 'mem', 'disk'):
        return f"{valor:.1f}%"
    if categoria == 'vpn':
        return f"{int(round(valor))} usuários"
    if categoria == 'net':
        # 'Bps'/bytes = bytes/s (converte p/ bits); 'bps' minúsculo = bits/s.
        # A distinção é case-sensitive, então usa a unidade ORIGINAL aqui.
        u_orig = (units or '').strip()
        eh_bytes = u_orig == 'Bps' or 'byte' in u_orig.lower() or u_orig == 'B'
        bits = valor * 8 if eh_bytes else valor
        mbps = bits / 1e6
        if mbps >= 1000: return f"{mbps/1000:.2f} Gbps"
        if mbps >= 1:    return f"{mbps:.1f} Mbps"
        return f"{bits/1e3:.0f} Kbps"
    return f"{valor:.2f} {units}".strip()


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

        rodape = (f"<div style='color:#666; font-size:12px; margin-top:10px;'>+ {ocultas} anomalia(s) de menor desvio omitida(s).</div>"
                  if ocultas else "")

        return cabecalho + resumo + tabela + rodape + "</div>"

    except Exception as e:
        return f"Erro na detecção de anomalias: {str(e)}"
