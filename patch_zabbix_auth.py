with open('/opt/oraculo-noc/app/services/zabbix.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

with open('/opt/oraculo-noc/app/services/zabbix.py', 'w', encoding='utf-8') as f:
    skip = False
    for line in lines:
        if line.startswith('def zabbix_api('):
            skip = True
            f.write('''def zabbix_api(method, params, token, url=None):
    if not token: raise ValueError("Token do Zabbix não configurado.")
    endpoint = url if url else ZABBIX_URL
    
    # 1. Tenta a forma tradicional do Zabbix 7.0 (Bearer Header)
    headers = {
        'Content-Type': 'application/json-rpc',
        'Authorization': f'Bearer {token}'
    }
    
    # 2. Injeta o token como "auth" no payload (Compatibilidade garantida Nginx/Apache)
    payload = {
        "jsonrpc": "2.0", 
        "method": method, 
        "params": params, 
        "auth": token, 
        "id": 1
    }
    
    import requests
    resposta = requests.post(endpoint, json=payload, headers=headers, timeout=45).json()
    if 'error' in resposta: 
        raise Exception(f"{resposta['error'].get('data')}")
    return resposta.get('result', [])
''')
            continue
        
        if skip:
            if line.strip() == "" or line.startswith('    ') or line.startswith('\t'):
                continue
            else:
                skip = False
        
        if not skip:
            f.write(line)

print("✅ Oráculo atualizado com a blindagem dupla de Autenticação Zabbix 7.0!")
