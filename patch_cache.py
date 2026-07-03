import os

caminho = '/opt/oraculo-noc/app/routes/chat.py'
if os.path.exists(caminho):
    with open(caminho, 'r', encoding='utf-8') as f:
        conteudo = f.read()
    
    # Injetar uma variável de tempo (timestamp) na renderização do template
    # Isto força o browser a tratar a página como conteúdo dinâmico e nunca fazer cache da estrutura
    import time
    timestamp = str(int(time.time()))
    
    if "render_template(" in conteudo and "cache_buster=" not in conteudo:
        conteudo = conteudo.replace(
            "render_template(", 
            f"render_template("
        )
        conteudo = conteudo.replace(
            "grupo_cliente=session.get('grupo_cliente')",
            "grupo_cliente=session.get('grupo_cliente'), cache_buster=str(__import__('time').time())"
        )
        
    with open(caminho, 'w', encoding='utf-8') as f:
        f.write(conteudo)
    print("✅ Cache busting implementado com sucesso!")
else:
    print("⚠️ Ficheiro chat.py não encontrado.")

# Agora atualizamos o template para incluir esse timestamp num local invisível
template_path = '/opt/oraculo-noc/app/templates/chat.html'
if os.path.exists(template_path):
    with open(template_path, 'r', encoding='utf-8') as f:
        html = f.read()
    
    if "<body" in html and "data-version" not in html:
        html = html.replace("<body", "<body data-version=\"{{ cache_buster }}\"")
        with open(template_path, 'w', encoding='utf-8') as f:
            f.write(html)
        print("✅ Template chat.html atualizado para forçar refresh no telemóvel.")
