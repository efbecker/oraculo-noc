import os

caminho = '/opt/oraculo-noc/app/services/secops.py'
if os.path.exists(caminho):
    with open(caminho, 'r', encoding='utf-8') as f:
        conteudo = f.read()
    
    # Substitui a linha do comando nmap por uma com timeout explícito de 10min e max-retries 1
    # Garante que ele não fique preso em firewalls mudos
    comando_velho = 'nmap -p- -sV'
    if "nmap -A" in conteudo:
        conteudo = conteudo.replace('f"nmap -A {target}"', 'f"nmap -A -T4 --max-retries 1 --host-timeout 10m {target}"')
    elif "nmap -p-" in conteudo:
        conteudo = conteudo.replace('f"nmap -p- -sV {target}"', 'f"nmap -A -T4 --max-retries 1 --host-timeout 10m {target}"')
        
    with open(caminho, 'w', encoding='utf-8') as f:
        f.write(conteudo)
    print("✅ SecOps atualizado com sucesso! Limite de 10 minutos ativo.")
else:
    print("⚠️ Arquivo secops.py não encontrado no caminho padrão.")
