"""
Branding — logo da empresa usado nos relatórios (SecOps e VMs/FinOps).

O logo é guardado em disco (instance/branding/) e sempre embutido como
data-URI base64 diretamente no HTML dos relatórios — assim os relatórios
continuam 100% autocontidos e portáteis (funcionam mesmo baixados e abertos
em outro lugar, sem depender do servidor do Oráculo estar no ar).
"""
import os
import base64
import mimetypes

from app import db

LOGO_DIR = os.path.join(db.INSTANCE_DIR, 'branding')
EXTENSOES_PERMITIDAS = {'.png', '.jpg', '.jpeg', '.webp', '.svg'}
TAMANHO_MAXIMO_BYTES = 2 * 1024 * 1024  # 2 MB é de sobra para um logo


def _arquivo_logo_atual():
    """Devolve o caminho do arquivo de logo atual, ou None se não houver."""
    if not os.path.isdir(LOGO_DIR):
        return None
    for nome in os.listdir(LOGO_DIR):
        caminho = os.path.join(LOGO_DIR, nome)
        if os.path.isfile(caminho) and os.path.splitext(nome)[1].lower() in EXTENSOES_PERMITIDAS:
            return caminho
    return None


def salvar_logo(file_storage):
    """Salva um novo logo (substitui o anterior, se houver). Retorna
    (ok: bool, mensagem: str)."""
    if not file_storage or not file_storage.filename:
        return False, "Nenhum arquivo enviado."

    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in EXTENSOES_PERMITIDAS:
        return False, f"Formato não suportado ({ext}). Use PNG, JPG, WEBP ou SVG."

    os.makedirs(LOGO_DIR, exist_ok=True)

    # Lê para validar o tamanho antes de gravar (evita logo gigante inflando
    # o HTML dos relatórios, que já ficam grandes com os gráficos embutidos).
    conteudo = file_storage.read()
    if len(conteudo) > TAMANHO_MAXIMO_BYTES:
        return False, f"Arquivo muito grande ({len(conteudo)/1024/1024:.1f} MB). Máximo: 2 MB."

    remover_logo()  # limpa qualquer logo anterior (evita acumular arquivos)
    destino = os.path.join(LOGO_DIR, f'logo{ext}')
    with open(destino, 'wb') as f:
        f.write(conteudo)

    return True, "✅ Logo salvo com sucesso."


def remover_logo():
    caminho = _arquivo_logo_atual()
    if caminho and os.path.exists(caminho):
        try:
            os.remove(caminho)
        except Exception:
            pass


def logo_info():
    """Para exibir no painel Admin: se existe logo, tamanho e um data-URI
    pronto para <img src="..."> de pré-visualização."""
    caminho = _arquivo_logo_atual()
    if not caminho:
        return {"existe": False}
    return {
        "existe": True,
        "nome": os.path.basename(caminho),
        "tamanho_kb": round(os.path.getsize(caminho) / 1024, 1),
        "data_uri": get_logo_data_uri(),
    }


def get_logo_data_uri():
    """Data-URI base64 do logo atual, pronto para embutir em <img src="...">
    dentro dos relatórios HTML. Devolve None se não houver logo configurado."""
    caminho = _arquivo_logo_atual()
    if not caminho:
        return None
    try:
        mime, _ = mimetypes.guess_type(caminho)
        mime = mime or 'image/png'
        with open(caminho, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('ascii')
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def get_logo_html(max_height_px=60, margin_bottom_px=15):
    """HTML pronto (tag <img>) para inserir no topo dos relatórios. Devolve
    string vazia se não houver logo — assim o chamador pode simplesmente
    concatenar sem precisar checar None em todo lugar."""
    uri = get_logo_data_uri()
    if not uri:
        return ""
    return (
        f'<div style="text-align:center; margin-bottom:{margin_bottom_px}px;">'
        f'<img src="{uri}" style="max-height:{max_height_px}px; max-width:280px; object-fit:contain;" alt="Logo">'
        f'</div>'
    )
