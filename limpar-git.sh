#!/usr/bin/env bash
#
# limpar-git.sh - Remove do rastreamento do git os arquivos que NUNCA deveriam
# estar versionados (segredos + estado de runtime), mantendo-os no disco.
#
# Rode UMA VEZ, dentro do repositorio, no servidor onde estao os arquivos reais:
#     cd /opt/oraculo-noc
#     bash limpar-git.sh
#     git push
#
# Depois disso, o .gitignore cuida de manter tudo limpo nos proximos commits.
#
# ATENCAO: este script tira os arquivos do rastreamento e do PROXIMO commit,
# mas eles CONTINUAM no HISTORICO antigo do git. Como o repo ja teve a senha do
# Postgres e a chave do Gemini commitadas, o correto e TAMBEM trocar esses dois
# segredos (a senha do banco e a chave da API) depois - eles devem ser
# considerados vazados. Ver instrucoes no final.

set -e

echo "==> Removendo do rastreamento (mantendo no disco)..."

# config.py real (tem SECRET_KEY / URL Zabbix / OCID)
git rm --cached config.py 2>/dev/null || echo "   config.py ja fora do git"

# arquivo com nome errado, se existir
git rm --cached .gitignoreu 2>/dev/null || echo "   .gitignoreu ja fora do git"

# instance/ inteiro: senha do Postgres, chave de IA, dumps .sql.gz, logo
git rm -r --cached instance/ 2>/dev/null || echo "   instance/ ja fora do git"

# logs
git rm -r --cached logs/ 2>/dev/null || echo "   logs/ ja fora do git"

# bancos SQLite e caches
git rm --cached oraculo_zabbix.db 2>/dev/null || echo "   oraculo_zabbix.db ja fora do git"
git rm -r --cached __pycache__/ 2>/dev/null || true
git rm -r --cached app/__pycache__/ 2>/dev/null || true
find . -path ./venv -prune -o -name '__pycache__' -type d -print 2>/dev/null | while read d; do
    git rm -r --cached "$d" 2>/dev/null || true
done

echo
echo "==> Removendo o config.py.example antigo do disco e regravando o correto..."
# (o .gitignore ja garante que config.py.example CONTINUA versionado)

echo
echo "==> Pronto para commitar. Revise com:  git status"
echo "    Depois:  git commit -m 'Limpa segredos e runtime do versionamento'"
echo "             git push"
echo
echo "########################################################################"
echo "# IMPORTANTE - TROQUE OS SEGREDOS QUE JA FORAM PARA O GIT:              #"
echo "#  1) Senha do PostgreSQL (usuario 'oraculo'):                          #"
echo "#     sudo -u postgres psql -c \"ALTER USER oraculo WITH PASSWORD 'NOVA';\" #"
echo "#     e atualize no painel Admin > Banco de Dados.                      #"
echo "#  2) Chave da API do Gemini: gere uma nova em aistudio.google.com,     #"
echo "#     revogue a antiga, e atualize em Admin > Inteligencia Artificial.  #"
echo "########################################################################"
