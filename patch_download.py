import os

caminho = '/opt/oraculo-noc/app/routes/chat.py'
with open(caminho, 'r', encoding='utf-8') as f:
    chat_py = f.read()

if "/api/download/<scan_id>" not in chat_py:
    download_route = """
@bp.route('/api/download/<scan_id>', methods=['GET'])
def download_report(scan_id):
    try:
        import sqlite3
        from config import DB_PATH
        import json
        from flask import Response
        import re
        from datetime import datetime
        
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            row = conn.execute("SELECT target, result_json FROM scans WHERE id = ?", (scan_id,)).fetchone()
            if not row or not row[1]: return "Relatório não disponível.", 404
            
            target = row[0]
            ai_text = json.loads(row[1]).get("ai_analysis", "")
            
            html_content = re.sub(r'\\*\\*(.*?)\\*\\*', r'<b>\\1</b>', ai_text)
            html_content = re.sub(r'### (.*?)\\n', r'<h3>\\1</h3>\\n', html_content)
            html_content = re.sub(r'## (.*?)\\n', r'<h2>\\1</h2>\\n', html_content)
            html_content = re.sub(r'# (.*?)\\n', r'<h1>\\1</h1>\\n', html_content)
            html_content = re.sub(r'\\[(.*?)\\]\\((.*?)\\)', r'<a href="\\2" target="_blank">\\1</a>', html_content)
            html_content = html_content.replace('\\n', '<br>')
            
            html = f\"\"\"
            <html><head><meta charset="UTF-8"><title>Relatório - {target}</title>
            <style>body{{font-family:sans-serif; padding:40px; line-height:1.6; color:#333; max-width:900px; margin:auto}} h1, h2, h3{{color:#0077ff}} .code{{background:#f4f4f4; padding:20px; border-radius:8px}} a{{color:#2ea043; font-weight:bold}}</style>
            </head><body>
            <h1>Oráculo SecOps - Relatório Executivo</h1>
            <hr><p><b>Alvo Auditado:</b> {target}<br><b>Data/Hora:</b> {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
            <div class='code'>{html_content}</div>
            <p style='text-align:center; font-size:0.8em; margin-top:50px'>Gerado por Oráculo Armazem Cloud</p>
            </body></html>
            \"\"\"
            
            return Response(
                html,
                mimetype="text/html",
                headers={"Content-disposition": f"attachment; filename=Relatorio_SecOps_{target}.html"}
            )
    except Exception as e:
        return f"Erro ao gerar relatório: {str(e)}", 500
"""
    chat_py += download_route
    with open(caminho, 'w', encoding='utf-8') as f:
        f.write(chat_py)
    print("✅ Rota de Download implementada com sucesso!")
