# Oráculo NOC

Assistente de NOC (Flask + Waitress, porta 5000) integrado ao Zabbix, com chat
de IA, relatórios FinOps de capacidade, previsão de capacidade, anomalias,
SecOps e backup Veeam.

## Deploy em uma linha (curl)

Em um servidor Debian/Ubuntu, como usuário com `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/efbecker/oraculo-noc/main/deploy.sh | sudo bash
```

O mesmo comando serve para **instalar** e para **atualizar**:

- Na primeira vez, clona o repositório em `/opt/oraculo-noc`, roda o
  `install.sh` (pacotes do sistema, virtualenv, dependências e serviço
  systemd `oraculo`) e sobe o serviço.
- Nas próximas, atualiza o código para a última versão da `main`, reinstala as
  dependências e reinicia o serviço. Arquivos de runtime não são tocados:
  `instance/` (segredos, chave de IA, senha do Postgres), `.env`, o banco
  SQLite (`*.db`), `logs/` e `venv/`.

Depois acesse `http://IP_DO_SERVIDOR:5000` (admin padrão: `admin` / `admin123`,
troque no primeiro acesso) e configure Banco de Dados, IA e usuários/tokens do
Zabbix pelo painel Admin.

### Opções

Passe as variáveis depois do `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/efbecker/oraculo-noc/main/deploy.sh \
  | sudo INSTALL_DIR=/srv/oraculo BRANCH=main bash
```

| Variável       | Padrão                                       | Para quê                                   |
|----------------|----------------------------------------------|--------------------------------------------|
| `INSTALL_DIR`  | `/opt/oraculo-noc`                           | Pasta da aplicação                         |
| `BRANCH`       | `main`                                       | Branch implantada                          |
| `REPO_URL`     | `https://github.com/efbecker/oraculo-noc.git`| Repositório (ex.: um fork)                 |
| `GITHUB_TOKEN` | —                                            | Token de leitura, se o repositório for privado |
| `RUN_USER`     | usuário que chamou o `sudo`                  | Usuário que roda o serviço                 |

### Repositório privado

Com o repositório privado, o `raw.githubusercontent.com` também exige token
(um *fine-grained token* com leitura de *Contents* basta). O token só é usado
nos comandos do git e não fica gravado no servidor:

```bash
TOKEN=ghp_xxx
curl -fsSL -H "Authorization: token $TOKEN" \
  https://raw.githubusercontent.com/efbecker/oraculo-noc/main/deploy.sh \
  | sudo GITHUB_TOKEN=$TOKEN bash
```

## Instalação manual (alternativa)

```bash
git clone https://github.com/efbecker/oraculo-noc.git
cd oraculo-noc
sudo bash install.sh
sudo systemctl start oraculo
```

## Operação

```bash
sudo systemctl status oraculo     # estado do serviço
journalctl -u oraculo -f          # logs ao vivo
sudo systemctl restart oraculo    # reinício
```

## Itens do Zabbix usados pelo relatório FinOps

O relatório reconhece os itens primeiro pela **chave** técnica e, como
respaldo, pelo **nome** do item. Nos templates de agente ativo
(`CLIENTE_*_LNX_AGENTE_ATIVO` / `CLIENTE_*_WIN_AGENTE_ATIVO`):

| Métrica            | Linux                                   | Windows                                                                    |
|--------------------|-----------------------------------------|----------------------------------------------------------------------------|
| vCPUs alocadas     | `system.cpu.num`                        | `wmi.get[root/cimv2,"Select NumberOfLogicalProcessors from Win32_ComputerSystem"]` |
| Uso de CPU (%)     | `system.cpu.util`                       | `system.cpu.util`                                                          |
| Memória total      | `vm.memory.size[total]`                 | `vm.memory.size[total]`                                                    |
| Uso de memória (%) | `vm.memory.size[pavailable]` (JS 100−x) | `vm.memory.util` (calculado)                                               |
| Discos             | `vfs.fs.size[{#FSNAME},pfree/used/...]` | `vfs.fs.size[{#FSNAME},pfree/used/...]`                                    |
| SO                 | `system.sw.os`                          | `system.uname`                                                             |

Em VMs monitoradas pela API do VMware, as vCPUs vêm de `vmware.vm.cpu.num`.
Sem chave conhecida, um item chamado "Quantidade de vCPUs" (ou "Number of
CPUs", "Número de núcleos"...) também é aceito. A capacidade em GHz é
`vCPUs × clock por vCPU` (cadastro do cliente em Admin > Usuários; padrão 2 GHz).
