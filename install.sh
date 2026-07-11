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

# --- 3. Configuracao ------------------------------------------------------
# A aplicacao NAO precisa mais de config.py. Todos os valores tem padrao no
# codigo (app/settings.py) e sao ajustaveis pelo painel Admin ou por variaveis
# de ambiente (arquivo .env opcional, lido pelo systemd). A SECRET_KEY e gerada
# e persistida automaticamente em instance/secret.key no primeiro boot.
info "Sem config.py para gerar — a app usa app/settings.py + painel Admin."

# Permissoes: o SECRET_KEY e os configs sensiveis ficam em instance/ (gerada
# no primeiro boot com chmod 600 pelo proprio codigo). Nada a ajustar aqui.

# --- 4. serviço systemd ----------------------------------------------------
# Garante que o usuario do servico consegue ESCREVER na pasta da aplicacao
# (SQLite, instance/, logs). Sem isso, se o clone foi feito como root e o
# servico roda como outro usuario, o boot falha com
# "sqlite3.OperationalError: unable to open database file".
info "Ajustando dono de ${APP_DIR} para ${RUN_USER}..."
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

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
info "Instalacao concluida! Proximos passos:"
echo "   1. Suba o servico:            sudo systemctl start ${SERVICE_NAME}"
echo "   2. Acompanhe os logs:         journalctl -u ${SERVICE_NAME} -f"
echo "   3. Acesse http://IP:5000 e entre com o admin padrao (admin / admin123)."
echo "   4. No painel Admin: troque a senha do admin, configure Banco de Dados,"
echo "      Inteligencia Artificial e cadastre os usuarios/tokens do Zabbix."
echo "   5. (Opcional) Fora da OCI, configure a API Key da OCI em ~/.oci/config."
echo
