"""
PROVEDORES DE IA PLUGÁVEIS — Oráculo AI
=======================================
Camada que permite trocar o "cérebro" do Oráculo pelo painel Admin, sem mexer
em código e sem reiniciar o serviço — mesmo padrão do módulo de banco (db.py):

    1) instance/ai_config.json  (gravado pelo painel Admin > Inteligência Artificial)
    2) Variáveis de ambiente:    AI_PROVIDER, AI_MODEL, AI_API_KEY, AI_BASE_URL
    3) Padrão: 'oci' (Oracle GenAI/Cohere) — comportamento original do Oráculo.

Provedores suportados (todos via REST com a lib 'requests', sem SDK novo):
    oci .............. Oracle OCI Generative AI (Cohere) — autentica por
                       ~/.oci/config (API key) ou Instance Principals (VM OCI)
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
MAX_TOKENS = 2048  # mesmo teto usado hoje com a Cohere
TEMPERATURA = 0.3  # mesma temperatura usada hoje

PROVIDER_LABELS = {
    'oci': 'Oracle OCI GenAI (Cohere)',
    'openai': 'OpenAI (GPT)',
    'gemini': 'Google Gemini',
    'anthropic': 'Anthropic (Claude)',
    'openai_compat': 'OpenAI-compatível (Groq, DeepSeek, Ollama...)',
}

# Modelo usado quando o campo "Modelo" fica em branco no painel.
# São sugestões estáveis; o painel tem uma lista de sugestões por provedor,
# mas QUALQUER string aceita pelo provedor pode ser digitada.
MODELO_PADRAO = {
    'oci': 'cohere.command-r-08-2024',
    'openai': 'gpt-4o-mini',
    'gemini': 'gemini-2.0-flash',
    'anthropic': 'claude-sonnet-4-5',
    'openai_compat': '',  # depende do servidor (ex.: 'llama3.1' no Ollama)
}

_state_lock = threading.Lock()


def _defaults_from_env():
    return {
        "provider": (os.environ.get('AI_PROVIDER') or 'oci').strip().lower(),
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
    provider = (provider or 'oci').strip().lower()
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
    cfg = {"provider": (provider or 'oci').strip().lower(), "model": (model or '').strip(),
           "api_key": (api_key or '').strip(), "base_url": (base_url or '').strip().rstrip('/')}
    try:
        resposta = chat("Responda somente com a palavra: OK", "Você é um teste de conectividade.",
                        [], cfg=cfg)
        modelo = cfg['model'] or MODELO_PADRAO.get(cfg['provider']) or '(padrão do servidor)'
        return True, f"✅ Conexão OK com {PROVIDER_LABELS.get(cfg['provider'], cfg['provider'])} · modelo '{modelo}' · resposta: {resposta[:60]}"
    except Exception as e:
        return False, f"Falha na conexão: {e}"


# ==========================================================================
# CHAT — despacho por provedor
# --------------------------------------------------------------------------
# history: lista de tuplas (role, content) com role em {'user', 'assistant'}
# ==========================================================================
def chat(prompt, preamble, history, modelo_usuario=None, cfg=None):
    if cfg is None:
        cfg = current_config(mask_key=False)
    provider = cfg.get('provider') or 'oci'

    # Modelo efetivo: campo do painel > modelo por usuário (legado, só faz
    # sentido na OCI, onde a coluna users.ai_model guarda ids Cohere) > padrão.
    model = cfg.get('model') or ''
    if not model and provider == 'oci' and modelo_usuario:
        model = modelo_usuario
    if not model:
        model = MODELO_PADRAO.get(provider, '')

    if provider == 'oci':
        return _chat_oci(prompt, preamble, history, model)
    if provider in ('openai', 'openai_compat'):
        return _chat_openai(prompt, preamble, history, model, cfg)
    if provider == 'gemini':
        return _chat_gemini(prompt, preamble, history, model, cfg)
    if provider == 'anthropic':
        return _chat_anthropic(prompt, preamble, history, model, cfg)
    raise ValueError(f"Provedor de IA desconhecido: {provider}")


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


def _chat_oci(prompt, preamble, history, model):
    """Oracle OCI GenAI (Cohere) — o modo original do Oráculo.
    Autentica por API key (~/.oci/config — funciona de qualquer datacenter)
    ou, em fallback, por Instance Principals (só dentro de uma VM da OCI)."""
    import oci  # import local: quem não usa OCI não precisa do pacote instalado
    from app.settings import OCI_ENDPOINT, OCI_COMPARTMENT_ID

    try:
        oci_cfg = oci.config.from_file()
        client = oci.generative_ai_inference.GenerativeAiInferenceClient(
            config=oci_cfg, service_endpoint=OCI_ENDPOINT, retry_strategy=oci.retry.NoneRetryStrategy()
        )
    except Exception:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        client = oci.generative_ai_inference.GenerativeAiInferenceClient(
            config={}, signer=signer, service_endpoint=OCI_ENDPOINT, retry_strategy=oci.retry.NoneRetryStrategy()
        )

    cohere_history = [
        oci.generative_ai_inference.models.CohereChatBotMessage(
            role=("USER" if role == 'user' else "CHATBOT"), message=content)
        for role, content in history
    ]
    chat_request = oci.generative_ai_inference.models.CohereChatRequest(
        api_format="COHERE", message=prompt, chat_history=cohere_history,
        preamble_override=preamble, max_tokens=MAX_TOKENS, temperature=TEMPERATURA
    )
    details = oci.generative_ai_inference.models.ChatDetails(
        compartment_id=OCI_COMPARTMENT_ID,
        serving_mode=oci.generative_ai_inference.models.OnDemandServingMode(model_id=model),
        chat_request=chat_request
    )
    return client.chat(details).data.chat_response.text


def _chat_openai(prompt, preamble, history, model, cfg):
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
        base = base or 'https://api.openai.com/v1'
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
                            "max_tokens": MAX_TOKENS, "temperature": TEMPERATURA})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()['choices'][0]['message']['content']


def _chat_gemini(prompt, preamble, history, model, cfg):
    """Google Gemini (Generative Language API)."""
    if not cfg.get('api_key'):
        raise ValueError("Informe a API Key do Google AI Studio.")
    contents = [{"role": ("user" if role == 'user' else "model"), "parts": [{"text": content}]}
                for role, content in _normalizar_alternancia(history)]
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": cfg['api_key']},
        timeout=TIMEOUT,
        json={"system_instruction": {"parts": [{"text": preamble}]},
              "contents": contents,
              "generationConfig": {"maxOutputTokens": MAX_TOKENS, "temperature": TEMPERATURA}})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()['candidates'][0]['content']['parts'][0]['text']


def _chat_anthropic(prompt, preamble, history, model, cfg):
    """Anthropic (Claude) — Messages API."""
    if not cfg.get('api_key'):
        raise ValueError("Informe a API Key da Anthropic.")
    messages = [{"role": ("user" if role == 'user' else "assistant"), "content": content}
                for role, content in _normalizar_alternancia(history)]
    messages.append({"role": "user", "content": prompt})

    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"Content-Type": "application/json",
                               "x-api-key": cfg['api_key'],
                               "anthropic-version": "2023-06-01"},
                      timeout=TIMEOUT,
                      json={"model": model, "system": preamble, "messages": messages,
                            "max_tokens": MAX_TOKENS, "temperature": TEMPERATURA})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()['content'][0]['text']
