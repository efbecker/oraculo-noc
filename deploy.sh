#!/usr/bin/env bash
#
# deploy.sh — Deploy do Oráculo NOC em uma linha, via curl
# ========================================================
# Instalação nova OU atualização de um servidor que já roda o Oráculo:
#
#     curl -fsSL https://raw.githubusercontent.com/efbecker/oraculo-noc/main/deploy.sh | sudo bash
#
# Repositório privado? Passe um token do GitHub (permissão de leitura):
#
#     curl -fsSL -H "Authorization: token SEU_TOKEN" \
#       https://raw.githubusercontent.com/efbecker/oraculo-noc/main/deploy.sh \
#       | sudo GITHUB_TOKEN=SEU_TOKEN bash
#
# Variáveis opcionais (passe depois do sudo, ex.: sudo BRANCH=dev bash):
#   INSTALL_DIR   pasta da aplicação          (padrão: /opt/oraculo-noc)
#   BRANCH        branch a implantar          (padrão: main)
#   REPO_URL      URL do repositório git      (padrão: github.com/efbecker/oraculo-noc)
#   GITHUB_TOKEN  token p/ repositório privado (não é gravado em disco)
#   RUN_USER      usuário do serviço          (padrão: quem chamou o sudo)
#
# O que ele faz (idempotente — pode rodar quantas vezes quiser):
#   1. Instala o git, se faltar.
#   2. Clona o repositório em INSTALL_DIR ou, se já existir, atualiza para a
#      última versão da BRANCH. Arquivos de runtime ficam intactos: instance/,
#      .env, banco SQLite (*.db), logs/ e venv/ estão no .gitignore.
#   3. Roda o install.sh (pacotes, venv, dependências, serviço systemd).
#   4. Reinicia o serviço e mostra o status.

set -euo pipefail

# Tudo dentro de main(): com "curl | bash" o bash lê o script do stdin;
# assim ele é lido INTEIRO antes de rodar, e nenhum comando (apt, git...)
# consome por engano o resto do script.
main() {

INSTALL_DIR="${INSTALL_DIR:-/opt/oraculo-noc}"
BRANCH="${BRANCH:-main}"
REPO_URL="${REPO_URL:-https://github.com/efbecker/oraculo-noc.git}"
SERVICE_NAME="oraculo"
export SUDO_USER="${RUN_USER:-${SUDO_USER:-root}}"

info() { echo -e "\033[1;36m[oráculo]\033[0m $*"; }
erro() { echo -e "\033[1;31m[erro]\033[0m $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    erro "Rode como root: curl -fsSL <url>/deploy.sh | sudo bash"
    exit 1
fi

# --- 1. git ----------------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    info "Instalando git..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git ca-certificates
fi

# Token só na linha de comando do git (nunca salvo no .git/config).
GIT_AUTH=()
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    GIT_AUTH=(-c "http.extraHeader=Authorization: Basic $(printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 | tr -d '\n')")
fi
# A pasta pertence ao usuário do serviço (o install.sh faz chown), mas o
# deploy roda como root: sem safe.directory o git recusa ("dubious ownership").
GIT=(git -c "safe.directory=$INSTALL_DIR" ${GIT_AUTH[@]+"${GIT_AUTH[@]}"})

# --- 2. código -------------------------------------------------------------
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Atualizando $INSTALL_DIR para a última versão de '$BRANCH'..."
    "${GIT[@]}" -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
    # reset --hard só mexe em arquivos VERSIONADOS. instance/, .env, *.db,
    # logs/ e venv/ são ignorados pelo git e continuam onde estão.
    "${GIT[@]}" -C "$INSTALL_DIR" checkout -q -B "$BRANCH" FETCH_HEAD
    "${GIT[@]}" -C "$INSTALL_DIR" reset -q --hard FETCH_HEAD
elif [[ -e "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
    erro "$INSTALL_DIR já existe e não é um clone git. Use outro INSTALL_DIR ou mova a pasta."
    exit 1
else
    info "Clonando $REPO_URL ($BRANCH) em $INSTALL_DIR..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    "${GIT[@]}" clone -q --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
info "Versão implantada: $("${GIT[@]}" -C "$INSTALL_DIR" log -1 --format='%h %s')"

# --- 3. instalação ---------------------------------------------------------
bash "$INSTALL_DIR/install.sh"

# --- 4. (re)inicia o serviço -----------------------------------------------
info "Reiniciando o serviço '$SERVICE_NAME'..."
systemctl restart "$SERVICE_NAME"
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
    info "Serviço no ar: http://$(hostname -I 2>/dev/null | awk '{print $1}'):5000"
else
    erro "O serviço não subiu. Veja os logs: journalctl -u $SERVICE_NAME -n 50 --no-pager"
    exit 1
fi
}

main "$@"
