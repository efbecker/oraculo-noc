"""
Configuracoes centrais do Oraculo NOC.
======================================
Substitui o antigo config.py, que era um arquivo OBRIGATORIO e fora do git —
causa recorrente de falha na subida ("ImportError: SECRET_KEY"). Aqui os valores
tem PADRAO sensato no proprio codigo, entao a aplicacao SEMPRE sobe, mesmo num
clone limpo, sem nenhum arquivo extra.

Precedencia de cada valor (do mais forte para o mais fraco):
  1. Variavel de ambiente (ex.: SECRET_KEY, ZABBIX_URL) — util para produao
     sem editar codigo; pode vir de um arquivo .env lido pelo systemd.
  2. Padrao definido aqui.

O que ANTES exigia config.py e agora nao exige mais:
  - SECRET_KEY .......... gerada aleatoria a cada boot se nao houver env var
                          (ver nota abaixo sobre sessoes).
  - ZABBIX_URL .......... cada usuario ja define a sua no painel Admin > Usuarios;
                          este e so o fallback global.
  - DB_PATH ............. caminho do SQLite (ignorado quando se usa PostgreSQL,
                          configurado no painel Admin > Banco de Dados).
"""
import os
import secrets

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ================= SECRET_KEY (sessoes de login) =================
# Se SECRET_KEY vier do ambiente, usa (recomendado em producao: valor fixo faz
# as sessoes sobreviverem a reinicios). Sem env var, gera uma aleatoria por boot
# — seguro, mas desloga todos a cada restart. Para fixar sem env var, o
# install.sh grava um SECRET_KEY no arquivo instance/secret.key na 1a execucao.
def _load_or_create_secret():
    env = os.environ.get('SECRET_KEY')
    if env:
        return env
    secret_file = os.path.join(BASE_DIR, 'instance', 'secret.key')
    try:
        if os.path.exists(secret_file):
            with open(secret_file, 'r', encoding='utf-8') as f:
                val = f.read().strip()
                if val:
                    return val
        # cria uma e persiste (sessoes sobrevivem a reinicios)
        os.makedirs(os.path.dirname(secret_file), exist_ok=True)
        val = secrets.token_hex(32)
        with open(secret_file, 'w', encoding='utf-8') as f:
            f.write(val)
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return val
    except OSError:
        # disco read-only ou sem permissao: cai no aleatorio em memoria
        return secrets.token_hex(32)


SECRET_KEY = _load_or_create_secret()


# ================= Banco de dados (SQLite fallback) =================
# Usado apenas quando o motor e SQLite. Ao configurar PostgreSQL no painel,
# este caminho e ignorado.
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'oraculo_zabbix.db'))


# ================= Zabbix (fallback global) =================
# Cada usuario pode ter a sua URL no painel Admin > Usuarios. Este e o padrao
# usado quando o usuario nao define uma URL propria. Pode ficar vazio: nesse
# caso, o usuario PRECISA ter uma URL propria configurada.
ZABBIX_URL = os.environ.get('ZABBIX_URL', '').strip()

