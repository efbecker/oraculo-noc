import os
path = '/opt/oraculo-noc/app/templates/chat.html'
with open(path, 'r', encoding='utf-8') as f:
    html = f.read()

if "function formatMarkdown(" not in html:
    markdown_func = """
        function formatMarkdown(text) {
            let html = text
                .replace(/```markdown|```json|```/g, "")
                .replace(/\\*\\*(.*?)\\*\\*/g, "<strong>$1</strong>")
                .replace(/### (.*?)\\n/g, "<h3 style='color:#00e5ff; margin:10px 0 5px 0;'>$1</h3>")
                .replace(/## (.*?)\\n/g, "<h2 style='color:#00e5ff; margin:15px 0 5px 0;'>$1</h2>")
                .replace(/- (.*?)\\n/g, "<li>$1</li>")
                .replace(/\\[(.*?)\\]\\((.*?)\\)/g, "<a href='$2' target='_blank' style='color:#ff9e00;'>$1</a>")
                .replace(/\\n/g, "<br>");
            return `<div class="ai-markdown" style="font-size:14px; line-height:1.5;">${html}</div>`;
        }
        
        function appendMessage(sender, content, isHtml = false, timestamp = '') {
            const msgDiv = document.createElement('div');
            msgDiv.classList.add(sender === 'user' ? 'msg-user' : 'msg-bot');
            
            // Aqui aplicamos o formatador de Markdown para as respostas da IA
            let finalContent = isHtml ? content : (sender === 'bot' ? formatMarkdown(content) : content.replace(/\\n/g, '<br>'));
            
            msgDiv.innerHTML = finalContent + (timestamp ? `<span class="msg-time">${timestamp}</span>` : '');
            chatMessages.appendChild(msgDiv);
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }
"""
    # Substitui a velha appendMessage
    import re
    html = re.sub(r'function appendMessage\(.*?\}.*?chatMessages\.scrollHeight;\s*\}', markdown_func, html, flags=re.DOTALL)
    
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)
    print("✅ Suporte a formatação Markdown estilo Red Team injetado!")
