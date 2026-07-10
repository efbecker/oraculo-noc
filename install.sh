#!/usr/bin/env bash
#
# install.sh — Instalador do Oráculo NOC
# =====================================
# Uso, logo após 'git clone':
#     cd oraculo-noc
#     sudo bash install.sh
#
# O que ele faz (é idempotente — pode rodar de novo sem quebrar nada):
#   1. Instala os pacotes de SISTEMA (python3, venv, nmap, etc.)
#   2. Cria um virtualenv em ./venv e instala o requirements.txt dentro dele
#   3. Gera o config.py a partir do config.py.example (com SECRET_KEY aleatória)
#   4. Instala o serviço systemd (rodando o Python do venv, com auto-restart)
#
# Não configura banco nem chave de IA: isso é feito depois, pelo painel Admin.

set -euo pipefail

# --- localização e usuário -------------------------------------------------
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="oraculo"
# Usuário que rodará o serviço: quem invocou o sudo (não o root), se possível.
RUN_USER="${SUDO_USER:-root}"

info() { echo -e "\033[1;36m[oráculo]\033[0m $*"; }
erro() { echo -e "\033[1;31m[erro]\033[0m $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    erro "Rode com sudo: sudo bash install.sh"
    exit 1
fi

# --- 1. pacotes de sistema -------------------------------------------------
info "Instalando pacotes de sistema..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# python3-venv: para o ambiente virtual | nmap: usado pelo Scan Profundo/SecOps
# build-essential + libpq-dev: para compilar o psycopg2 (driver do PostgreSQL)
apt-get install -y -qq python3 python3-venv python3-pip nmap build-essential libpq-dev

# --- 2. virtualenv + dependências Python -----------------------------------
# Boa prática: as dependências do projeto ficam ISOLADAS em ./venv, sem poluir
# o Python do sistema nem exigir o --break-system-packages.
info "Criando o ambiente virtual em ./venv..."
if [[ ! -d "$APP_DIR/venv" ]]; then
    python3 -m venv "$APP_DIR/venv"
fi
info "Instalando dependências Python (isso pode demorar um pouco)..."
"$APP_DIR/venv/bin/pip" install --upgrade pip -q
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# --- 3. config.py ----------------------------------------------------------
# Gera o config.py real a partir do modelo versionado. Regenera se o arquivo
# nao existir, estiver VAZIO, ou nao for um Python valido (evita o caso de um
# config.py vazio/quebrado ter sido commitado por engano no repositorio).
PRECISA_GERAR=0
if [[ ! -s "$APP_DIR/config.py" ]]; then
    PRECISA_GERAR=1   # nao existe ou tem 0 bytes
elif ! "$APP_DIR/venv/bin/python" -c "import ast,sys; ast.parse(open('$APP_DIR/config.py').read())" 2>/dev/null; then
    PRECISA_GERAR=1   # existe mas nao compila
    info "config.py existente e invalido — sera regenerado (backup em config.py.bak)."
    cp "$APP_DIR/config.py" "$APP_DIR/config.py.bak"
fi

if [[ "$PRECISA_GERAR" -eq 1 ]]; then
    info "Gerando config.py a partir do modelo, com SECRET_KEY aleatoria..."
    SECRET="$("$APP_DIR/venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"
    sed "s|TROQUE-por-uma-chave-aleatoria-longa|${SECRET}|" \
        "$APP_DIR/config.py.example" > "$APP_DIR/config.py"
    echo
    erro "IMPORTANTE: revise o config.py e ajuste OCI_COMPARTMENT_ID e ZABBIX_URL"
    erro "antes de usar em producao (os valores vieram do modelo de exemplo)."
    echo
else
    info "config.py ja existe e e valido — mantido como esta."
fi

# Permissões: config.py tem infra sensível (SECRET_KEY) → só o dono lê.
chown "$RUN_USER":"$RUN_USER" "$APP_DIR/config.py"
chmod 600 "$APP_DIR/config.py"

# --- 4. serviço systemd ----------------------------------------------------
# Gera o unit apontando para o Python do VENV e para o diretório real do clone.
# Ajuste MemoryMax conforme a RAM da VM (regra: ~70% da RAM total).
info "Instalando o serviço systemd '$SERVICE_NAME'..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Oraculo NOC (Flask + Waitress na porta 5000)
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/run.py
EnvironmentFile=-${APP_DIR}/.env
Restart=always
RestartSec=5
MemoryMax=3G
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

echo
info "Instalação concluída! Próximos passos:"
echo "   1. Revise o config.py:        nano ${APP_DIR}/config.py"
echo "   2. (Fora da OCI) configure a API Key:  ~/.oci/config  — ver MIGRACAO.md"
echo "   3. Suba o serviço:            sudo systemctl start ${SERVICE_NAME}"
echo "   4. Acompanhe os logs:         journalctl -u ${SERVICE_NAME} -f"
echo "   5. No painel Admin: configure Banco de Dados e Inteligência Artificial."
echo
