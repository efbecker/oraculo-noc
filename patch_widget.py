import os
import re

# 1. Adicionar API para ler a percentagem na routes/chat.py
chat_routes_path = '/opt/oraculo-noc/app/routes/chat.py'
with open(chat_routes_path, 'r', encoding='utf-8') as f:
    chat_py = f.read()

if "def get_active_scans():" not in chat_py:
    api_scan = """
@bp.route('/api/active_scans', methods=['GET'])
def get_active_scans():
    try:
        import sqlite3
        from config import DB_PATH
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT id, target, progress, stage FROM scans WHERE status='running'").fetchall()
            return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify([])
"""
    chat_py += api_scan
    with open(chat_routes_path, 'w', encoding='utf-8') as f:
        f.write(chat_py)

# 2. Injetar a tua Widget Original no chat.html
html_path = '/opt/oraculo-noc/app/templates/chat.html'
with open(html_path, 'r', encoding='utf-8') as f:
    html = f.read()

# Substituir o CSS da barra rasca pelo CSS do teu Widget Lindo
old_css = """.secops-card { background: #1a1010; border: 1px solid #ff4d4d; border-radius: 8px; padding: 15px; margin-top: 10px; display: flex; flex-direction: column; gap: 10px; box-shadow: 0 0 15px rgba(255,77,77,0.1); width: 100%; max-width: 500px; box-sizing: border-box; }
        .secops-header { display: flex; justify-content: space-between; font-size: 13px; font-weight: bold; color: #ff4d4d; flex-wrap: wrap; gap: 5px; }
        .secops-bg { background: #2a1515; height: 6px; border-radius: 4px; overflow: hidden; width: 100%; position: relative; }
        .secops-fill { background: linear-gradient(90deg, #ff4d4d, #ff9e00); height: 100%; width: 5%; transition: width 8s ease-in-out; }
        .secops-status-text { font-size: 11px; color: #00e5ff; font-family: monospace; }"""

new_css = """/* SCAN WIDGET DO EDUARDO (ORIGINAL) */
        .scan-widget { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 18px; margin-top: 10px; width: 100%; max-width: 450px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);}
        .scan-header { display: flex; justify-content: space-between; align-items: center; font-size: 0.85rem; margin-bottom: 15px; flex-wrap: wrap; gap: 12px;}
        .scan-target { font-weight: bold; color: #e0e0e0; word-break: break-word; max-width: 100%; }
        .scan-stage { display: flex; align-items: center; gap: 10px; justify-content: flex-end; flex-grow: 1; }
        .progress-container { width:100%; background-color:rgba(255,255,255,0.05); border-radius:6px; height:8px; overflow:hidden; position:relative; }
        .progress-bar { height:100%; width:0%; background:linear-gradient(90deg, #00e5ff, #3fb950); transition:width 0.4s ease; }"""
html = html.replace(old_css, new_css)

# Substituir o JS do Radar Antigo pela lógica do teu Código (Active Scans)
old_js = """const nmapPhases = [
            { text: "> Ping Scan: Verificando se o host está ativo...", width: "15%" },
            { text: "> SYN Stealth Scan: Identificando portas abertas...", width: "35%" },
            { text: "> Service Scan: Sondando banners de serviço (-sV)...", width: "55%" },
            { text: "> OS Detection: Analisando assinaturas TCP/IP...", width: "70%" },
            { text: "> NSE Scripts: Executando scripts de vulnerabilidade...", width: "85%" },
            { text: "> NSE Scripts: Testes avançados em andamento...", width: "95%" },
            { text: "> Compilando relatório final de auditoria...", width: "98%" }
        ];

        function showSecOpsBar(target) {
            if (document.getElementById('secopsBarCard')) return; 
            const wrapper = document.createElement('div');
            wrapper.classList.add('msg-bot');
            wrapper.innerHTML = `
            <div class="secops-card" id="secopsBarCard">
                <div class="secops-header">
                    <span><i class="fas fa-shield-alt fa-spin"></i> Varredura Nmap</span>
                    <span>Alvo: ${target}</span>
                </div>
                <div class="secops-bg">
                    <div class="secops-fill" id="secopsFillBar"></div>
                </div>
                <div class="secops-status-text" id="secopsPhaseText">> Inicializando motor Nmap...</div>
            </div>`;
            chatMessages.appendChild(wrapper);
            chatMessages.scrollTop = chatMessages.scrollHeight;

            let phaseIndex = 0;
            const textElement = document.getElementById('secopsPhaseText');
            const fillBar = document.getElementById('secopsFillBar');
            
            if (textInterval) clearInterval(textInterval);
            textInterval = setInterval(() => {
                if (textElement && fillBar && phaseIndex < nmapPhases.length) {
                    textElement.innerText = nmapPhases[phaseIndex].text;
                    fillBar.style.width = nmapPhases[phaseIndex].width;
                    phaseIndex++;
                }
            }, 8000); 
        }

        function removeSecOpsBar() {
            const bar = document.getElementById('secopsBarCard');
            if (bar) bar.closest('.msg-bot').remove();
            if (textInterval) clearInterval(textInterval);
        }

        function startRadar() {
            if (pollingInterval) clearInterval(pollingInterval);
            pollingInterval = setInterval(async () => {
                try {
                    const response = await fetch('/api/chat/history');
                    const messages = await response.json();
                    if (messages.length > 0) {
                        const lastMsg = messages[messages.length - 1];
                        if (lastMsg.sender === 'bot' && (lastMsg.content.includes("Nmap scan report") || lastMsg.content.includes("Portas abertas") || lastMsg.content.includes("vulnerabilidade"))) {
                            clearInterval(pollingInterval);
                            removeSecOpsBar();
                            loadHistory(); 
                        }
                    }
                } catch (e) { console.error("Erro radar:", e); }
            }, 10000); 
        }"""

new_js = """// WIDGET DO EDUARDO (Lê o banco de dados em tempo real!)
        let activeScans = {};
        function createScanWidget(scanId, target, stage="Inicializando...", progress=0) {
            if(document.getElementById(`scan-${scanId}`)) return;
            const html = `
            <div class="scan-widget" id="scan-${scanId}">
                <div class="scan-header">
                    <span class="scan-target"><i class="fas fa-crosshairs"></i> ${target}</span>
                    <span class="scan-stage">
                        <span id="stage-text-${scanId}" style="color: #888; font-size: 0.8rem; font-family: monospace;">${stage}</span>
                        <span id="stage-pct-${scanId}" style="background: rgba(0,229,255,0.15); color: #00e5ff; padding: 2px 8px; border-radius: 12px; font-weight: bold; font-size: 0.85rem;">${progress}%</span>
                    </span>
                </div>
                <div class="progress-container">
                    <div class="progress-bar" id="bar-${scanId}" style="width: ${progress}%"></div>
                </div>
            </div>`;
            const wrapper = document.createElement('div');
            wrapper.classList.add('msg-bot');
            wrapper.innerHTML = html;
            chatMessages.appendChild(wrapper);
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }

        function startRadar() {
            if (pollingInterval) return;
            pollingInterval = setInterval(async () => {
                if (Object.keys(activeScans).length === 0) { clearInterval(pollingInterval); pollingInterval = null; return; }
                try {
                    const res = await fetch('/api/active_scans');
                    const scans = await res.json();
                    for (let scanId in activeScans) {
                        const activeData = scans.find(s => s.id === scanId);
                        if (activeData) {
                            const pct = Math.round(activeData.progress);
                            activeScans[scanId].progress = pct; activeScans[scanId].stage = activeData.stage;
                            
                            const textEl = document.getElementById(`stage-text-${scanId}`); if (textEl) textEl.innerText = activeData.stage || "Analisando...";
                            const pctEl = document.getElementById(`stage-pct-${scanId}`); if (pctEl) pctEl.innerText = `${pct}%`;
                            const barEl = document.getElementById(`bar-${scanId}`); if (barEl) barEl.style.width = `${pct}%`;
                        } else {
                            // Se sumiu do banco, é porque o scan acabou! Mata o widget e puxa o chat novo.
                            const barEl = document.getElementById(`bar-${scanId}`); if (barEl) { barEl.style.width = `100%`; barEl.style.background = `#3fb950`; }
                            delete activeScans[scanId];
                            loadHistory(); // Atualiza a tela com o texto final
                        }
                    }
                } catch (e) {}
            }, 2000); // Polling ultra rápido de 2s para ver a barra crescer a sério!
        }"""
html = html.replace(old_js, new_js)

# Substituir a injeção ao fazer o envio da mensagem:
html = html.replace("showSecOpsBar(match ? match[0] : \"Automático\");", "// Widget desenhado via API do Servidor")
html = html.replace("if (data.action === 'scan') { startRadar(); }", """if (data.action === 'scan') { 
                        activeScans[data.scan_id] = { target: data.target, stage: "Iniciando Nmap...", progress: 0 };
                        createScanWidget(data.scan_id, data.target, "Iniciando Nmap...", 0);
                        startRadar(); 
                    }""")

# Substituir a lógica de LoadHistory
html = html.replace("""if (isScanRunning) {
                    showSecOpsBar(lastTarget);
                    startRadar();
                }""", """// Restaurador de Scans Ativos pós Refresh
                try {
                    const res = await fetch('/api/active_scans');
                    const scans = await res.json();
                    scans.forEach(scan => {
                        activeScans[scan.id] = { target: scan.target, stage: scan.stage || "Analisando...", progress: Math.round(scan.progress || 0) };
                        createScanWidget(scan.id, scan.target, activeScans[scan.id].stage, activeScans[scan.id].progress);
                    });
                    if (scans.length > 0) startRadar();
                } catch(e) {}""")

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html)
print("✅ Widget Original do Eduardo reativado com sucesso!")
