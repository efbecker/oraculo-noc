"""
PREVISÃO DE CAPACIDADE (regressão linear) — Oráculo AI
======================================================
Ajusta uma reta (mínimos quadrados) sobre o histórico de trends e projeta o
esgotamento de recursos ANTES de qualquer trigger disparar:

- DISCOS ......... projeção de quando o uso chega a 100%
- CPU / MEMÓRIA .. projeção de quando o uso SUSTENTADO cruza 90% (limiar de risco)
- INTERFACES ..... ritmo de crescimento do tráfego e tempo para DOBRAR o volume
                   (sem depender de conhecer a banda contratada do link, que o
                   Zabbix geralmente não sabe — o "tempo para dobrar" é a métrica
                   honesta de tendência para planejamento de link)

Inspirado no capacity_forecast do initMAX/zabbix-mcp-server, implementado
nativamente sobre o motor do Oráculo (fetch adaptativo + escopo multi-tenant).
"""
import re
import time
from datetime import datetime

from app.services.zabbix import (
    fetch_historical_data,
    _coletar_hosts_e_itens,
    _parse_disk_key,
)

MIN_PONTOS = 48        # ~2 dias de trends: mínimo para uma reta minimamente confiável
LIMIAR_PCT_DIA = 0.02  # crescimento < 0.02%/dia é ruído, não tendência
ALVO_CPU_MEM = 90.0    # % em que CPU/memória sustentadas viram risco operacional

_TH = "padding:10px; text-align:left; color:#00e5ff; font-size:11px;"
_TD = "padding:10px; border-bottom:1px solid #333;"


def _regressao_linear(xs, ys):
    """Mínimos quadrados simples. Retorna (inclinação, intercepto).
    xs em DIAS (float) para a inclinação sair direto em unidade/dia."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return slope, my - slope * mx


def _serie_do_item(hist, itemid):
    """Extrai (xs em dias, ys) dos trends de um item, ou None se insuficiente."""
    rows = hist.get(itemid)
    if not rows or len(rows) < MIN_PONTOS:
        return None
    t0 = int(rows[0]['clock'])
    xs = [(int(r['clock']) - t0) / 86400.0 for r in rows]
    ys = [float(r.get('value_avg', r.get('value', 0))) for r in rows]
    return xs, ys


def _cor_prazo(dias_restantes):
    if dias_restantes is None: return '#00e676'
    if dias_restantes <= 30:   return '#ff4d4d'
    if dias_restantes <= 90:   return '#ff9e00'
    return '#00e676'


def _fmt_prazo(agora, dias_restantes):
    if dias_restantes is None: return None
    if dias_restantes > 730:   return "&gt; 2 anos"
    data = datetime.fromtimestamp(agora + dias_restantes * 86400).strftime('%d/%m/%Y')
    return f"~{int(round(dias_restantes))} dias ({data})"


def _fmt_mbps(bits_por_seg):
    mbps = bits_por_seg / 1e6
    if mbps >= 1000: return f"{mbps/1000:.2f} Gbps"
    if mbps >= 1:    return f"{mbps:.1f} Mbps"
    return f"{bits_por_seg/1e3:.0f} Kbps"


def _eh_bytes(units):
    u = (units or '').strip()
    return u == 'Bps' or u == 'B' or 'byte' in u.lower()


def _limpar_nome_iface(nome):
    """Reduz o nome do item de tráfego a um identificador de interface legível."""
    limpo = re.sub(r'(?i)\b(número de|numero de|in|out|inbound|outbound|rx|tx|receive[a-z]*|sent|'
                   r'transmit[a-z]*|recebidos|transmitidos|entrada|sa[ií]da|download|upload|'
                   r'traffic|tráfego|trafego|rate|bits?|bytes?|bps|octets?|per second|na interface)\b', '', nome)
    limpo = re.sub(r'^[^\w]+|[^\w]+$', '', limpo).strip(' -:[]()')
    return limpo or "Principal"


def prever_capacidade_zabbix(dias_historico: float, token: str, grupo: str, url: str = None) -> str:
    dias = max(7, min(int(float(dias_historico)), 90))
    agora = int(time.time())
    inicio = agora - dias * 24 * 3600

    try:
        hosts, items = _coletar_hosts_e_itens(token, grupo, url)
        if not hosts: return "Nenhum servidor encontrado no grupo."
        nome_host = {h['hostid']: h['name'] for h in hosts}

        # ------------------------------------------------------------------
        # CLASSIFICAÇÃO DOS ALVOS
        # ------------------------------------------------------------------
        alvos_disco = {}   # (hostid, mount) -> item  [prefere pused; pfree = 100 - v]
        alvos_cpu, alvos_mem, alvos_net = [], [], []

        for it in items:
            nl = it['name'].lower()
            kl = (it.get('key_') or '').lower()
            units = (it.get('units') or '').strip()

            mount, kind = _parse_disk_key(it.get('key_', ''))
            if mount and kind in ('pused', 'pfree'):
                mount_norm = mount.strip().strip('\'"').rstrip('\\/') or mount.strip()
                chave = (it['hostid'], mount_norm)
                if chave not in alvos_disco or kind == 'pused':
                    it['_mount'], it['_kind'] = mount_norm, kind
                    alvos_disco[chave] = it
                continue

            # CPU e memória: só em %, senão a projeção para 90% não faz sentido.
            # Chaves VMware percentuais entram por CHAVE (robusto a idioma do
            # template); os demais entram por nome+unidade.
            eh_cpu_pct = 'vmware.vm.cpu.usage.perf' in kl or (
                '%' in units and 'cpu' in nl and re.search(r'\b(uso|usage|util|percent)\b', nl))
            eh_mem_pct = ('vmware.vm.memory.usage[' in kl or kl.startswith('vmware.vm.memory.usage')) or (
                '%' in units and re.search(r'\b(mem|memória|memory)\b', nl) and re.search(r'\b(uso|usage|util|percent)\b', nl))

            if eh_cpu_pct and 'mhz' not in nl and 'ghz' not in nl:
                alvos_cpu.append(it)
            elif eh_mem_pct:
                alvos_mem.append(it)
            elif (not re.search(r'\b(loopback|lo|veth|docker|speed|velocidade|drop|error|discard|status|estado)\b', nl)
                    and not re.search(r'\b(disco|disk|iops|leitura|escrita|lidos?|escritos?|gravad\w*|read|write|written)\b', nl)
                    and re.search(r'\b(bits?|bytes?|traffic|tráfego|trafego|bps|octets?)\b', nl)):
                alvos_net.append(it)

        alvos_disco = list(alvos_disco.values())
        todos = alvos_disco + alvos_cpu + alvos_mem + alvos_net
        if not todos:
            return ("Não encontrei métricas elegíveis (discos em %, CPU/memória em % ou tráfego de rede) "
                    "para projetar capacidade. Verifique se os templates expõem esses itens.")

        # Uma ÚNICA passada adaptativa para todo o histórico (à prova de timeout)
        hist = fetch_historical_data(todos, inicio, agora, dias, token, url)

        # ------------------------------------------------------------------
        # 1) DISCOS — projeção para 100%
        # ------------------------------------------------------------------
        res_disco = []
        for it in alvos_disco:
            serie = _serie_do_item(hist, it['itemid'])
            if not serie: continue
            xs, ys = serie
            if it['_kind'] == 'pfree':
                ys = [100.0 - y for y in ys]
            slope, _ = _regressao_linear(xs, ys)
            atual = ys[-1]
            restam = (100.0 - atual) / slope if (slope > LIMIAR_PCT_DIA and atual < 100) else None
            res_disco.append({'host': nome_host.get(it['hostid'], '?'), 'alvo': it['_mount'],
                              'atual': atual, 'slope': slope, 'restam': restam})

        # ------------------------------------------------------------------
        # 2) CPU e 3) MEMÓRIA — projeção para 90% sustentados
        # ------------------------------------------------------------------
        def projetar_percentual(alvos):
            saida = []
            for it in alvos:
                serie = _serie_do_item(hist, it['itemid'])
                if not serie: continue
                xs, ys = serie
                slope, _ = _regressao_linear(xs, ys)
                atual = ys[-1]
                if atual >= ALVO_CPU_MEM:
                    restam = 0.0
                elif slope > LIMIAR_PCT_DIA:
                    restam = (ALVO_CPU_MEM - atual) / slope
                else:
                    restam = None
                saida.append({'host': nome_host.get(it['hostid'], '?'), 'alvo': it['name'],
                              'atual': atual, 'slope': slope, 'restam': restam})
            return saida

        res_cpu = projetar_percentual(alvos_cpu)
        res_mem = projetar_percentual(alvos_mem)

        # ------------------------------------------------------------------
        # 4) INTERFACES — ritmo de crescimento + tempo para DOBRAR o tráfego
        # ------------------------------------------------------------------
        res_net = []
        for it in alvos_net:
            serie = _serie_do_item(hist, it['itemid'])
            if not serie: continue
            xs, ys = serie
            fator = 8.0 if _eh_bytes(it.get('units')) else 1.0  # normaliza tudo para BITS/s
            ys = [y * fator for y in ys]
            slope, _ = _regressao_linear(xs, ys)   # bits/s a mais POR DIA
            media = sum(ys) / len(ys)
            pico = max(ys)
            # Crescimento significativo: > 2% da média por dia E > 100 Kbps/dia
            # (evita apontar "crescimento" em links ociosos oscilando no ruído)
            cresce = slope > max(0.02 * media, 100_000.0)
            dobra = (media / slope) if (cresce and media > 0) else None
            res_net.append({'host': nome_host.get(it['hostid'], '?'),
                            'alvo': _limpar_nome_iface(it['name']),
                            'sentido': '↓ IN' if re.search(r'(?i)\b(in|rx|receb|entrada|download)\b', it['name']) else '↑ OUT',
                            'media': media, 'pico': pico, 'slope': slope,
                            'cresce': cresce, 'dobra': dobra})

        # ==================================================================
        # MONTAGEM DO HTML (mesmo estilo dark dos demais relatórios)
        # ==================================================================
        n_proj = len(res_disco) + len(res_cpu) + len(res_mem) + len(res_net)
        cabecalho = (
            f"<div style='background:#1e1e1e; padding:20px; border-radius:8px; border:1px solid #333; font-family:sans-serif;'>"
            f"<h3 style='color:#00e5ff; margin:0 0 5px 0;'>📈 Previsão de Capacidade (Oráculo AI)</h3>"
            f"<div style='color:#888; font-size:13px; margin-bottom:15px;'>Regressão linear sobre os últimos {dias} dias · "
            f"{len(hosts)} hosts · {n_proj} recursos projetados · Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}</div>"
        )
        if n_proj == 0:
            return (cabecalho + "<div style='color:#ccc;'>Os recursos encontrados ainda não têm histórico suficiente "
                    "(mínimo ~2 dias de trends) para uma projeção confiável.</div></div>")

        # ---- Resumo executivo global -------------------------------------
        criticos = [r for r in (res_disco + res_cpu + res_mem) if r['restam'] is not None and r['restam'] <= 30]
        atencao = [r for r in (res_disco + res_cpu + res_mem) if r['restam'] is not None and 30 < r['restam'] <= 90]
        if criticos:
            resumo = (f"<div style='background:#2b1616; border-left:4px solid #ff4d4d; padding:12px 15px; border-radius:6px; color:#ccc; margin-bottom:20px;'>"
                      f"🚨 <strong style='color:#ff4d4d;'>{len(criticos)} recurso(s) esgotam em até 30 dias!</strong> "
                      f"Planeje expansão ou limpeza imediatamente. Outros {len(atencao)} em até 90 dias.</div>")
        elif atencao:
            resumo = (f"<div style='background:#2b2416; border-left:4px solid #ff9e00; padding:12px 15px; border-radius:6px; color:#ccc; margin-bottom:20px;'>"
                      f"⚠️ <strong style='color:#ff9e00;'>{len(atencao)} recurso(s) com esgotamento projetado em até 90 dias.</strong> "
                      f"Nenhum caso crítico (&lt;30 dias).</div>")
        else:
            resumo = ("<div style='background:#12261a; border-left:4px solid #00e676; padding:12px 15px; border-radius:6px; color:#ccc; margin-bottom:20px;'>"
                      "✅ <strong style='color:#00e676;'>Capacidade saudável.</strong> Nenhum recurso com projeção de "
                      "esgotamento nos próximos 90 dias no ritmo atual.</div>")

        def secao_percentual(titulo, resultados, alvo_pct, rotulo_alvo, teto_linhas=40):
            if not resultados:
                return ""
            resultados.sort(key=lambda r: (r['restam'] if r['restam'] is not None else 9e9, -r['atual']))
            estaveis = [r for r in resultados if r['restam'] is None]
            com_proj = [r for r in resultados if r['restam'] is not None]
            # CPU/mem: mostra só quem tem tendência; discos mostram tudo (limitado)
            exibir = resultados[:teto_linhas] if rotulo_alvo == "100%" else (com_proj[:teto_linhas] or resultados[:10])
            ocultos = len(resultados) - len(exibir)
            linhas = []
            for r in exibir:
                cor = _cor_prazo(r['restam'])
                if r['restam'] is None:
                    previsao = 'Reduzindo' if r['slope'] < -LIMIAR_PCT_DIA else 'Estável'
                elif r['restam'] == 0.0:
                    previsao = f"JÁ ACIMA DE {rotulo_alvo}"
                    cor = '#ff4d4d'
                else:
                    previsao = _fmt_prazo(agora, r['restam'])
                cor_uso = '#ff4d4d' if r['atual'] >= alvo_pct else ('#ff9e00' if r['atual'] >= alvo_pct - 10 else '#e0e0e0')
                linhas.append(
                    f"<tr><td style='{_TD} color:#fff; font-weight:bold;'>{r['host']}</td>"
                    f"<td style='{_TD} color:#ccc; font-family:monospace;'>{r['alvo']}</td>"
                    f"<td style='{_TD} color:{cor_uso}; font-family:monospace; font-weight:bold;'>{r['atual']:.1f}%</td>"
                    f"<td style='{_TD} color:#888; font-family:monospace;'>{r['slope']:+.2f}%/dia</td>"
                    f"<td style='{_TD} color:{cor}; font-family:monospace; font-weight:bold;'>{previsao}</td></tr>"
                )
            nota_estaveis = (f"<div style='color:#666; font-size:12px; margin:8px 0 0 0;'>"
                             f"{len(estaveis)} recurso(s) estáveis/sem tendência de alta"
                             f"{f' · +{ocultos} linha(s) omitida(s)' if ocultos > 0 else ''}.</div>"
                             if (estaveis or ocultos > 0) else "")
            return (f"<h4 style='color:#e0e0e0; margin:25px 0 10px 0; border-bottom:1px solid #333; padding-bottom:6px;'>{titulo}</h4>"
                    f"<table style='width:100%; border-collapse:collapse; font-size:13px;'>"
                    f"<thead><tr style='background:#242424;'>"
                    f"<th style='{_TH}'>HOST</th><th style='{_TH}'>RECURSO</th><th style='{_TH}'>USO ATUAL</th>"
                    f"<th style='{_TH}'>TENDÊNCIA</th><th style='{_TH}'>PROJEÇÃO ({rotulo_alvo})</th>"
                    f"</tr></thead><tbody>" + "".join(linhas) + "</tbody></table>" + nota_estaveis)

        def secao_interfaces(resultados, teto_linhas=25):
            if not resultados:
                return ""
            # Exibe TODAS as que crescem + as maiores por volume (mesmo estáveis):
            # links de firewall/WAN são de alto volume e o NOC quer vê-los sempre,
            # ainda que a projeção seja "Estável".
            crescendo = sorted([r for r in resultados if r['cresce']],
                               key=lambda r: (r['dobra'] if r['dobra'] is not None else 9e9))
            estaveis_top = sorted([r for r in resultados if not r['cresce']],
                                  key=lambda r: -r['media'])
            exibir = (crescendo + estaveis_top)[:teto_linhas]
            linhas = []
            for r in exibir:
                if r['cresce']:
                    cor = '#ff4d4d' if (r['dobra'] and r['dobra'] <= 60) else '#ff9e00'
                    proj = f"Tráfego DOBRA em ~{int(round(r['dobra']))} dias" if r['dobra'] else "Crescendo"
                    tend = f"+{_fmt_mbps(r['slope'])}/dia"
                else:
                    cor, proj, tend = '#00e676', 'Estável', f"{'+' if r['slope']>=0 else ''}{_fmt_mbps(abs(r['slope']))}/dia"
                linhas.append(
                    f"<tr><td style='{_TD} color:#fff; font-weight:bold;'>{r['host']}</td>"
                    f"<td style='{_TD} color:#ccc;'>{r['alvo']} <span style='color:#888; font-size:11px;'>{r['sentido']}</span></td>"
                    f"<td style='{_TD} color:#e0e0e0; font-family:monospace;'>{_fmt_mbps(r['media'])}</td>"
                    f"<td style='{_TD} color:#e0e0e0; font-family:monospace;'>{_fmt_mbps(r['pico'])}</td>"
                    f"<td style='{_TD} color:#888; font-family:monospace;'>{tend}</td>"
                    f"<td style='{_TD} color:{cor}; font-family:monospace; font-weight:bold;'>{proj}</td></tr>"
                )
            titulo_extra = "" if crescendo else " <span style='color:#666; font-size:12px;'>(nenhuma em crescimento)</span>"
            omitidas = len(resultados) - len(exibir)
            nota = (f"<div style='color:#666; font-size:12px; margin:8px 0 0 0;'>"
                    f"{f'{omitidas} interface(s) de menor volume omitida(s). ' if omitidas > 0 else ''}"
                    f"A projeção usa o tempo para o tráfego médio DOBRAR — como o Zabbix normalmente não conhece a banda "
                    f"contratada do link, essa é a referência honesta para planejamento.</div>")
            return (f"<h4 style='color:#e0e0e0; margin:25px 0 10px 0; border-bottom:1px solid #333; padding-bottom:6px;'>🌐 Interfaces de Rede (Firewalls e Servidores){titulo_extra}</h4>"
                    f"<table style='width:100%; border-collapse:collapse; font-size:13px;'>"
                    f"<thead><tr style='background:#242424;'>"
                    f"<th style='{_TH}'>HOST</th><th style='{_TH}'>INTERFACE</th><th style='{_TH}'>MÉDIA ({dias}D)</th>"
                    f"<th style='{_TH}'>PICO</th><th style='{_TH}'>TENDÊNCIA</th><th style='{_TH}'>PROJEÇÃO</th>"
                    f"</tr></thead><tbody>" + "".join(linhas) + "</tbody></table>" + nota)

        corpo = (
            secao_percentual("💾 Discos", res_disco, 90.0, "100%") +
            secao_percentual("⚙️ CPU", res_cpu, ALVO_CPU_MEM, f"{ALVO_CPU_MEM:.0f}%") +
            secao_percentual("🧠 Memória", res_mem, ALVO_CPU_MEM, f"{ALVO_CPU_MEM:.0f}%") +
            secao_interfaces(res_net)
        )

        rodape = ("<div style='color:#666; font-size:12px; margin-top:15px;'>"
                  "Projeções lineares com base no ritmo do período analisado — mudanças bruscas de uso "
                  "(deploys, expurgos, migrações) alteram a data real.</div>")

        return cabecalho + resumo + corpo + rodape + "</div>"

    except Exception as e:
        return f"Erro na previsão de capacidade: {str(e)}"

