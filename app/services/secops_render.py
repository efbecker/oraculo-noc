"""
RENDERIZADOR DO LAUDO SECOPS — Markdown → HTML
==============================================
O laudo do SecOps é escrito pela IA em Markdown (listas com marcadores, sub-itens
recuados, cabeçalhos '##', rótulos em **negrito**). O conversor antigo (uma pilha
de regex em chat.py) NÃO tratava listas: os marcadores '* ' e '- ' vazavam como
asteriscos literais e o recuo dos sub-itens sumia — o laudo ficava "cru".

Este módulo faz uma conversão de verdade, linha a linha:
  - cabeçalhos '#'..'###'          → <h1>..<h3>
  - listas com marcador '-'/'*'    → <ul><li>, com aninhamento por indentação
  - **negrito**                    → <b>
  - [texto](url)                   → <a> (só http/https)
  - CVE-AAAA-NNNN                   → link clicável para o NVD  (requisito do cliente:
                                      MANTER os links dos CVEs)
  - "CVSS 9.8"                      → selo colorido por faixa de severidade
  - CRÍTICO/ALTO/MÉDIO/BAIXO e
    Severidade Crítica/Alta/...    → selos coloridos ("pills")

Também extrai o "Risco Geral" para desenhar um banner de destaque no topo.
Tudo é escapado antes de receber tags (evita quebrar o HTML se um banner do Nmap
trouxer '<', '>' ou '&').
"""
import re
import html as _html

# ---- Paleta por severidade (tema escuro) -------------------------------------
_SEV_CORES = {
    "critico":       ("#ff5252", "#1a1a1a"),
    "alto":          ("#ff9800", "#1a1a1a"),
    "medio":         ("#ffd54f", "#1a1a1a"),
    "baixo":         ("#66bb6a", "#0d0d0d"),
    "indeterminada": ("#78909c", "#ffffff"),
    "informativa":   ("#78909c", "#ffffff"),
}

# Normalização "com acento / sem acento / gênero" → chave da paleta
_SEV_ALIASES = {
    "critico": "critico", "critica": "critico",
    "alto": "alto", "alta": "alto",
    "medio": "medio", "media": "medio",
    "baixo": "baixo", "baixa": "baixo",
    "indeterminada": "indeterminada", "indeterminado": "indeterminada",
    "informativa": "informativa", "informativo": "informativa",
}


def _sem_acento(s):
    tabela = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc")
    return s.lower().translate(tabela)


def _pill(rotulo, chave):
    fg = _SEV_CORES.get(chave, ("#78909c", "#fff"))[0]
    txt = _SEV_CORES.get(chave, ("#78909c", "#fff"))[1]
    return (
        f'<span class="sev-pill" style="background:{fg}; color:{txt};">'
        f'{rotulo}</span>'
    )


def _cvss_pill(score_str):
    try:
        s = float(score_str)
    except ValueError:
        return f"CVSS {score_str}"
    if s >= 9.0:
        chave = "critico"
    elif s >= 7.0:
        chave = "alto"
    elif s >= 4.0:
        chave = "medio"
    elif s > 0.0:
        chave = "baixo"
    else:
        chave = "informativa"
    return _pill(f"CVSS {score_str}", chave)


def _inline(text):
    """Formatação inline (aplicada a texto JÁ escapado no nível do bloco)."""
    t = text

    # **negrito**
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)

    # links markdown [texto](http...)
    t = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        t,
    )

    # CVE → NVD (mantém os links dos CVEs, requisito do cliente)
    t = re.sub(
        r"\bCVE-\d{4}-\d{4,7}\b",
        lambda m: (
            f'<a class="cve-link" href="https://nvd.nist.gov/vuln/detail/{m.group(0)}"'
            f' target="_blank" rel="noopener">{m.group(0)}</a>'
        ),
        t,
    )

    # "CVSS 9.8" → selo colorido
    t = re.sub(r"\bCVSS\s+(\d{1,2}\.\d)\b", lambda m: _cvss_pill(m.group(1)), t)

    # Níveis de risco em CAIXA ALTA (usados no "Risco Geral")
    def _caps(m):
        chave = _SEV_ALIASES.get(_sem_acento(m.group(0)), "indeterminada")
        return _pill(m.group(0), chave)

    t = re.sub(r"\b(CR[IÍ]TICO|ALTO|M[EÉ]DIO|BAIXO)\b", _caps, t)

    # "Severidade Crítica/Alta/Média/Baixa/Indeterminada/Informativa"
    def _sev(m):
        palavra = m.group(1)
        chave = _SEV_ALIASES.get(_sem_acento(palavra), "indeterminada")
        return "Severidade " + _pill(palavra, chave)

    t = re.sub(
        r"\bSeveridade\s+(Cr[ií]tica|Alta|M[ée]dia|Baixa|Indeterminada|Informativa)\b",
        _sev,
        t,
    )
    return t


def _extrair_risco(md_text):
    """Localiza a classificação de 'Risco Geral' para desenhar o banner do topo."""
    m = re.search(
        r"Risco\s+Geral[^\n]*?\b(CR[IÍ]TICO|ALTO|M[EÉ]DIO|BAIXO)\b",
        md_text,
        re.IGNORECASE,
    )
    if not m:
        return None
    nivel = m.group(1).upper()
    chave = _SEV_ALIASES.get(_sem_acento(nivel), "indeterminada")
    cor = _SEV_CORES.get(chave, ("#78909c", "#fff"))[0]
    return (nivel, cor)


def render_report_html(md_text):
    """Converte o Markdown do laudo em HTML já com listas, selos e links."""
    if not md_text:
        return "<p>Relatório vazio.</p>"

    linhas = md_text.replace("\r\n", "\n").split("\n")
    out = []
    pilha = []  # indentações das <ul> abertas

    def fecha_ate(indent):
        while pilha and pilha[-1] > indent:
            out.append("</ul>")
            pilha.pop()

    def fecha_tudo():
        while pilha:
            out.append("</ul>")
            pilha.pop()

    for bruto in linhas:
        # Bullet? espaços + (- ou *) + espaço + conteúdo
        mb = re.match(r"^(\s*)[-*]\s+(.*)$", bruto)
        if mb:
            indent = len(mb.group(1).replace("\t", "    "))
            conteudo = _inline(_html.escape(mb.group(2), quote=False))
            fecha_ate(indent)
            if not pilha or pilha[-1] < indent:
                out.append("<ul>")
                pilha.append(indent)
            out.append(f"<li>{conteudo}</li>")
            continue

        # Não é bullet: fecha listas abertas
        fecha_tudo()
        texto = bruto.strip()
        if not texto:
            continue

        mh = re.match(r"^(#{1,6})\s+(.*)$", texto)
        if mh:
            nivel = min(len(mh.group(1)), 3)
            out.append(f"<h{nivel}>{_inline(_html.escape(mh.group(2), quote=False))}</h{nivel}>")
            continue

        out.append(f"<p>{_inline(_html.escape(texto, quote=False))}</p>")

    fecha_tudo()
    corpo = "\n".join(out)

    risco = _extrair_risco(md_text)
    if risco:
        nivel, cor = risco
        banner = (
            f'<div class="risk-banner" style="border-left:6px solid {cor};">'
            f'<span class="risk-banner-label">Risco Geral do Alvo</span>'
            f'<span class="risk-banner-value" style="color:{cor};">{nivel}</span>'
            f"</div>"
        )
        corpo = banner + corpo

    return corpo


# CSS específico do laudo (injetado no <style> do relatório).
REPORT_EXTRA_CSS = """
    .content-box ul { margin: 8px 0 8px 4px; padding-left: 22px; }
    .content-box li { margin: 5px 0; }
    .content-box ul ul { margin: 4px 0; }
    .content-box p { margin: 10px 0; }
    .sev-pill { display:inline-block; padding:2px 9px; border-radius:11px;
                font-size:12px; font-weight:700; letter-spacing:.3px;
                vertical-align:middle; }
    .cve-link { color:#ff6b6b !important; font-weight:bold; }
    .risk-banner { display:flex; align-items:center; justify-content:space-between;
                   background:#141414; border:1px solid #2a2a2a; border-radius:8px;
                   padding:16px 22px; margin-bottom:22px; }
    .risk-banner-label { color:#9aa0a6; font-size:13px; text-transform:uppercase;
                         letter-spacing:1px; }
    .risk-banner-value { font-size:24px; font-weight:800; letter-spacing:1px; }
"""
