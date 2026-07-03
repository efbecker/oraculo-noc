with open('/opt/oraculo-noc/app/services/zabbix.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

with open('/opt/oraculo-noc/app/services/zabbix.py', 'w', encoding='utf-8') as f:
    skip = False
    for line in lines:
        if line.startswith('def zabbix_api('):
            skip = True
            f.write('''def zabbix_api(method, params, token, url=None):
    if not token: raise ValueError("Token do Zabbix não configurado para este usuário.")
    endpoint = url if url else ZABBIX_URL
    
    # ZABBIX 7.0+: O Token viaja no Header como Bearer!
    headers = {
        'Content-Type': 'application/json-rpc',
        'Authorization': f'Bearer {token}'
    }
    
    payload = {
        "jsonrpc": "2.0", 
        "method": method, 
        "params": params, 
        "id": 1
    }
    
    resposta = requests.post(endpoint, json=payload, headers=headers, timeout=45).json()
    if 'error' in resposta: raise Exception(f"Erro na API Zabbix: {resposta['error'].get('data')}")
    return resposta.get('result', [])
''')
            continue
        
        if skip:
            # Pula as linhas antigas da função até achar a próxima função (que não tem recuo de espaço)
            if line.strip() == "" or line.startswith('    ') or line.startswith('\t'):
                continue
            else:
                skip = False
        
        if not skip:
            f.write(line)

print("✅ Oráculo atualizado para autenticação Zabbix 7.0 (Bearer Token)!")
