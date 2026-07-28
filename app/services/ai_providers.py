"""
PROVEDORES DE IA PLUGÁVEIS — Oráculo AI
=======================================
Camada que permite trocar o "cérebro" do Oráculo pelo painel Admin, sem mexer
em código e sem reiniciar o serviço — mesmo padrão do módulo de banco (db.py):

    1) instance/ai_config.json  (gravado pelo painel Admin > Inteligência Artificial)
    2) Variáveis de ambiente:    AI_PROVIDER, AI_MODEL, AI_API_KEY, AI_BASE_URL
    3) Sem configuração: NENHUM provedor é assumido. O chat avisa o usuário que
       é preciso configurar uma IA no painel (nada de erro técnico na cara dele).

Provedores suportados (todos via REST com a lib 'requests', sem SDK):
    openai ........... OpenAI (GPT) — Bearer token
    gemini ........... Google Gemini — chave via header x-goog-api-key
    anthropic ........ Anthropic (Claude) — x-api-key
    openai_compat .... Qualquer API compatível com OpenAI (Groq, DeepSeek,
                       Mistral, Ollama local etc.) — exige Base URL
"""
import os
import json
import threading
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
AI_CONFIG_FILE = os.path.join(INSTANCE_DIR, 'ai_config.json')

TIMEOUT = 90       # segundos por chamada de IA
MAX_TOKENS = 2048  # teto de tokens por resposta
TEMPERATURA = 0.3  # mesma temperatura usada hoje

PROVIDER_LABELS = {
    'openai': 'OpenAI (GPT)',
    'gemini': 'Google Gemini',
    'anthropic': 'Anthropic (Claude)',
    'openai_compat': 'OpenAI-compatível (Groq, DeepSeek, Ollama...)',
}

# Modelo usado quando o campo "Modelo" fica em branco no painel.
# São sugestões estáveis; o painel tem uma lista de sugestões por provedor,
# mas QUALQUER string aceita pelo provedor pode ser digitada.
MODELO_PADRAO = {
    'openai': 'gpt-4o-mini',
    'gemini': 'gemini-2.0-flash',
    'anthropic': 'claude-sonnet-4-5',
    'openai_compat': '',  # depende do servidor (ex.: 'llama3.1' no Ollama)
}

# Endpoint padrão de cada provedor. TODOS são editáveis no painel (campo
# "Base URL"): se o provedor mudar a URL, ou se você usar um gateway/proxy
# corporativo (LiteLLM, Azure OpenAI, um mirror interno), basta trocar aqui
# pela interface — sem tocar em código.
BASE_URL_PADRAO = {
    'openai': 'https://api.openai.com/v1',
    'gemini': 'https://generativelanguage.googleapis.com/v1beta',
    'anthropic': 'https://api.anthropic.com/v1',
    'openai_compat': '',   # obrigatório informar (ex.: http://10.0.0.5:11434/v1)
}

_state_lock = threading.Lock()


def _defaults_from_env():
    return {
        "provider": (os.environ.get('AI_PROVIDER') or '').strip().lower(),
        "model": (os.environ.get('AI_MODEL') or '').strip(),
        "api_key": (os.environ.get('AI_API_KEY') or '').strip(),
        "base_url": (os.environ.get('AI_BASE_URL') or '').strip(),
    }


def _load_state():
    """Config efetiva: arquivo (se existir) tem prioridade sobre env vars."""
    cfg = _defaults_from_env()
    try:
        if os.path.exists(AI_CONFIG_FILE):
            with open(AI_CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            cfg.update({k: v for k, v in saved.items() if v is not None})
    except Exception:
        pass  # arquivo corrompido/ilegível: segue só com env vars
    return cfg


_STATE = _load_state()


def current_config(mask_key=True):
    """Config atual. Por padrão a chave vem MASCARADA (para exibir no painel)
    — a chave real nunca deve voltar para o navegador."""
    with _state_lock:
        cfg = dict(_STATE)
    if mask_key:
        k = cfg.get('api_key') or ''
        cfg['api_key'] = (f"••••••••{k[-4:]}" if len(k) > 8 else ('••••' if k else ''))
    return cfg


def reconfigure(provider, model='', api_key='', base_url='', persist=True):
    """Aplica a nova configuração em memória (vale IMEDIATAMENTE para as
    próximas mensagens de chat, sem reiniciar) e, se persist, grava em
    instance/ai_config.json com permissão restrita (contém a chave!)."""
    provider = (provider or '').strip().lower()
    if provider not in PROVIDER_LABELS:
        raise ValueError(f"Provedor desconhecido: {provider}")
    with _state_lock:
        _STATE['provider'] = provider
        _STATE['model'] = (model or '').strip()
        _STATE['api_key'] = (api_key or '').strip()
        _STATE['base_url'] = (base_url or '').strip().rstrip('/')
        snapshot = dict(_STATE)
    if persist:
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        with open(AI_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, indent=2)
        try:
            os.chmod(AI_CONFIG_FILE, 0o600)
        except Exception:
            pass


def test_connection(provider, model='', api_key='', base_url=''):
    """Faz uma pergunta REAL ao provedor informado (sem salvar nada) e devolve
    (ok, mensagem). Usado pelo botão 'Testar Conexão' do painel."""
    cfg = {"provider": (provider or '').strip().lower(), "model": (model or '').strip(),
           "api_key": (api_key or '').strip(), "base_url": (base_url or '').strip().rstrip('/')}
    try:
        resposta = chat("Responda somente com a palavra: OK", "Você é um teste de conectividade.",
                        [], cfg=cfg)
        modelo = cfg['model'] or MODELO_PADRAO.get(cfg['provider']) or '(padrão do servidor)'
        return True, f"✅ Conexão OK com {PROVIDER_LABELS.get(cfg['provider'], cfg['provider'])} · modelo '{modelo}' · resposta: {resposta[:60]}"
    except Exception as e:
        return False, f"Falha na conexão: {e}"


# ==========================================================================
# PERFIS DE IA (multi-tenant): uma IA diferente por cliente
# --------------------------------------------------------------------------
# O painel guarda N perfis nomeados (ex.: "Gemini-Pollux", "GPT-ASV",
# "Claude-INLOG"), cada um com provedor/modelo/chave/URL próprios. Cada usuário
# pode ser vinculado a um perfil (coluna users.ai_profile). Quem não tiver perfil
# definido usa o perfil PADRÃO (a config global, que é o comportamento atual).
#
# Por que perfis em vez de colar a chave direto no cadastro do usuário?
#  - Uma chave por CLIENTE, não por usuário: vários usuários do mesmo cliente
#    compartilham o perfil sem duplicar (e sem re-digitar) a chave.
#  - Trocar a chave do Gemini = editar 1 perfil, não N usuários.
#  - A chave continua num só lugar seguro (instance/, chmod 600), nunca no
#    banco nem no navegador.
# ==========================================================================
PROFILES_FILE = os.path.join(INSTANCE_DIR, 'ai_profiles.json')
_profiles_lock = threading.Lock()


def _load_profiles():
    try:
        if os.path.exists(PROFILES_FILE):
            with open(PROFILES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


_PROFILES = _load_profiles()


def _save_profiles(snapshot):
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    with open(PROFILES_FILE, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, indent=2)
    try:
        os.chmod(PROFILES_FILE, 0o600)  # contém chaves de API!
    except Exception:
        pass


def list_profiles(mask_key=True):
    """Lista os perfis salvos. Chaves mascaradas por padrão (para o painel)."""
    with _profiles_lock:
        perfis = {k: dict(v) for k, v in _PROFILES.items()}
    if mask_key:
        for p in perfis.values():
            k = p.get('api_key') or ''
            p['api_key'] = (f"••••••••{k[-4:]}" if len(k) > 8 else ('••••' if k else ''))
    return perfis


def save_profile(nome, provider, model='', api_key='', base_url=''):
    """Cria/atualiza um perfil. api_key em branco = mantém a chave atual do
    perfil (mesma regra dos demais campos sensíveis do painel)."""
    nome = (nome or '').strip()
    if not nome:
        raise ValueError("Informe um nome para o perfil (ex.: 'Gemini-Pollux').")
    provider = (provider or '').strip().lower()
    if provider not in PROVIDER_LABELS:
        raise ValueError(f"Provedor desconhecido: {provider}")
    with _profiles_lock:
        atual = _PROFILES.get(nome, {})
        chave = (api_key or '').strip() or (atual.get('api_key') or '')
        _PROFILES[nome] = {
            'provider': provider,
            'model': (model or '').strip(),
            'api_key': chave,
            'base_url': (base_url or '').strip().rstrip('/'),
        }
        snapshot = {k: dict(v) for k, v in _PROFILES.items()}
    _save_profiles(snapshot)


def delete_profile(nome):
    with _profiles_lock:
        if nome in _PROFILES:
            del _PROFILES[nome]
            snapshot = {k: dict(v) for k, v in _PROFILES.items()}
        else:
            return False
    _save_profiles(snapshot)
    return True


def config_for_user(username):
    """Resolve a configuração de IA que ESTE usuário deve usar:
       1) o perfil vinculado a ele (users.ai_profile), se existir e for válido;
       2) senão, a config global (padrão) do painel.
    Nunca levanta exceção: qualquer problema cai no padrão global."""
    try:
        from app import db
        row = db.query_one("SELECT ai_profile FROM users WHERE username=?", (username,))
        nome = (row['ai_profile'] or '').strip() if row and row.get('ai_profile') else ''
        if nome:
            with _profiles_lock:
                perfil = _PROFILES.get(nome)
            if perfil:
                return dict(perfil)
    except Exception:
        pass  # coluna ausente, banco fora, perfil apagado: usa o global
    return current_config(mask_key=False)


def label_for_user(username):
    """Rótulo curto e amigável da IA que ESTE usuário usa — para os logs, no
    lugar do antigo '[IA Oracle]' fixo. Reflete o que está configurado em
    Admin > Inteligência Artificial (provedor + modelo, ou o perfil do cliente).
    Ex.: 'Google Gemini · gemini-2.0-flash'. NUNCA inclui a chave de API e nunca
    levanta exceção — se algo falhar, devolve 'IA não configurada'."""
    try:
        cfg = config_for_user(username)
        provider = (cfg.get('provider') or '').strip().lower()
        if not provider:
            return 'IA não configurada'
        label = PROVIDER_LABELS.get(provider, provider)
        modelo = (cfg.get('model') or '').strip() or MODELO_PADRAO.get(provider, '') or '(modelo padrão)'
        return f"{label} · {modelo}"
    except Exception:
        return 'IA não configurada'


# ==========================================================================
# CHAT — despacho por provedor
# --------------------------------------------------------------------------
# history: lista de tuplas (role, content) com role em {'user', 'assistant'}
# ==========================================================================
class IANaoConfiguradaError(Exception):
    """A conta não tem nenhuma IA configurada (nem perfil próprio, nem padrão
    global). Não é uma falha técnica — é configuração faltando. O chat traduz
    isso numa orientação amigável em vez de mostrar stack trace ao cliente."""
    pass


def chat(prompt, preamble, history, modelo_usuario=None, cfg=None, max_tokens=None):
    # max_tokens: teto de tokens SÓ para esta chamada. Fica em None no chat comum
    # (usa o MAX_TOKENS global de 2048); relatórios longos — como o SecOps — pedem
    # um teto maior para não serem cortados no meio (era o que truncava o laudo).
    if cfg is None:
        cfg = current_config(mask_key=False)
    provider = (cfg.get('provider') or '').strip().lower()

    # SEM provedor definido: não assumimos nenhum provedor (antes havia um padrão, e
    # clientes sem perfil caíam nela sem querer, recebendo um erro técnico).
    if not provider:
        raise IANaoConfiguradaError(
            "Nenhuma IA está configurada para esta conta."
        )
    if provider not in PROVIDER_LABELS:
        raise IANaoConfiguradaError(
            f"O provedor de IA '{provider}' não existe mais ou não é suportado. "
            f"Escolha um provedor válido no painel."
        )
    # Provedores por HTTP exigem chave (exceto servidores locais tipo Ollama,
    # que aceitam base_url sem chave).
    if not (cfg.get('api_key') or '').strip() and provider != 'openai_compat':
        raise IANaoConfiguradaError(
            f"A IA está definida como {PROVIDER_LABELS[provider]}, mas nenhuma "
            f"chave de API foi cadastrada."
        )

    model = (cfg.get('model') or '').strip() or MODELO_PADRAO.get(provider, '')

    if provider in ('openai', 'openai_compat'):
        return _chat_openai(prompt, preamble, history, model, cfg, max_tokens)
    if provider == 'gemini':
        return _chat_gemini(prompt, preamble, history, model, cfg, max_tokens)
    if provider == 'anthropic':
        return _chat_anthropic(prompt, preamble, history, model, cfg, max_tokens)
    raise IANaoConfiguradaError(f"Provedor de IA desconhecido: {provider}")


def _normalizar_alternancia(history):
    """Anthropic e Gemini EXIGEM alternância estrita user/assistant no
    histórico, começando por 'user'. O histórico do Oráculo pode violar isso
    (ex.: intro de relatório + aviso de erro = duas mensagens seguidas do
    assistente, pois as respostas HTML são puladas) — sem esta normalização,
    a API devolve HTTP 400 no meio de uma conversa aparentemente normal.
    Regras: mescla mensagens consecutivas do mesmo papel em uma só e, se a
    conversa começar pelo assistente, ancora com uma mensagem de usuário."""
    norm = []
    for role, content in history:
        if norm and norm[-1][0] == role:
            norm[-1] = (role, norm[-1][1] + "\n\n" + content)
        else:
            norm.append((role, content))
    if norm and norm[0][0] != 'user':
        norm.insert(0, ('user', '[Início da conversa]'))
    return norm


def _chat_openai(prompt, preamble, history, model, cfg, max_tokens=None):
    """OpenAI oficial OU qualquer API compatível (Groq, DeepSeek, Mistral,
    Ollama local...). Para compatíveis, a Base URL é obrigatória —
    ex.: http://10.0.0.50:11434/v1 (Ollama) ou https://api.groq.com/openai/v1."""
    base = (cfg.get('base_url') or '').rstrip('/')
    if cfg.get('provider') == 'openai_compat':
        if not base:
            raise ValueError("Para 'OpenAI-compatível', informe a Base URL (ex.: http://IP:11434/v1 no Ollama).")
        if not model:
            raise ValueError("Para 'OpenAI-compatível', informe o nome do modelo (ex.: llama3.1).")
    else:
        base = base or BASE_URL_PADRAO['openai']
        if not cfg.get('api_key'):
            raise ValueError("Informe a API Key da OpenAI.")

    headers = {"Content-Type": "application/json"}
    if cfg.get('api_key'):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    messages = [{"role": "system", "content": preamble}]
    messages += [{"role": ("user" if role == 'user' else "assistant"), "content": content}
                 for role, content in history]
    messages.append({"role": "user", "content": prompt})

    r = requests.post(f"{base}/chat/completions", headers=headers, timeout=TIMEOUT,
                      json={"model": model, "messages": messages,
                            "max_tokens": (max_tokens or MAX_TOKENS), "temperature": TEMPERATURA})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()['choices'][0]['message']['content']


def _chat_gemini(prompt, preamble, history, model, cfg, max_tokens=None):
    """Google Gemini (Generative Language API)."""
    if not cfg.get('api_key'):
        raise ValueError("Informe a API Key do Google AI Studio.")
    contents = [{"role": ("user" if role == 'user' else "model"), "parts": [{"text": content}]}
                for role, content in _normalizar_alternancia(history)]
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    base = (cfg.get('base_url') or '').rstrip('/') or BASE_URL_PADRAO['gemini']
    r = requests.post(
        f"{base}/models/{model}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": cfg['api_key']},
        timeout=TIMEOUT,
        json={"system_instruction": {"parts": [{"text": preamble}]},
              "contents": contents,
              "generationConfig": {"maxOutputTokens": (max_tokens or MAX_TOKENS), "temperature": TEMPERATURA}})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()['candidates'][0]['content']['parts'][0]['text']


def _chat_anthropic(prompt, preamble, history, model, cfg, max_tokens=None):
    """Anthropic (Claude) — Messages API."""
    if not cfg.get('api_key'):
        raise ValueError("Informe a API Key da Anthropic.")
    messages = [{"role": ("user" if role == 'user' else "assistant"), "content": content}
                for role, content in _normalizar_alternancia(history)]
    messages.append({"role": "user", "content": prompt})

    base = (cfg.get('base_url') or '').rstrip('/') or BASE_URL_PADRAO['anthropic']
    r = requests.post(f"{base}/messages",
                      headers={"Content-Type": "application/json",
                               "x-api-key": cfg['api_key'],
                               "anthropic-version": "2023-06-01"},
                      timeout=TIMEOUT,
                      json={"model": model, "system": preamble, "messages": messages,
                            "max_tokens": (max_tokens or MAX_TOKENS), "temperature": TEMPERATURA})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()['content'][0]['text']
