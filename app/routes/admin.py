import os
import secrets
from werkzeug.security import generate_password_hash
from flask import Blueprint, request, redirect, url_for, session, render_template_string, jsonify, send_file
from app import db
from app.services import backup_manager, branding, ai_providers

bp = Blueprint('admin', __name__)


@bp.app_template_filter('format_size')
def format_size(size_bytes):
    """Formata bytes na unidade mais legível (B/KB/MB), em vez de forçar MB
    fixo — um backup de 8 KB virava '0.0 MB' e parecia vazio/quebrado."""
    if not size_bytes:
        return "-"
    size_bytes = float(size_bytes)
    if size_bytes < 1024:
        return f"{int(size_bytes)} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / 1024 / 1024:.1f} MB"

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Admin - Oráculo</title>
    <style>
        body { background-color: #121212; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 15px; margin: 0; }
        .container { max-width: 1200px; margin: auto; background: #1e1e1e; padding: 25px; border-radius: 8px; border: 1px solid #333; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 15px; margin-bottom: 25px; flex-wrap: wrap; gap: 15px; }
        .header h1 { color: #00e5ff; margin: 0; font-size: 24px; }
        
        /* Formulário em Grid Responsivo (Perfeito para Celular) */
        .form-container { background: #161616; padding: 20px; border-radius: 6px; border: 1px solid #333; margin-bottom: 30px; }
        .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .form-group { display: flex; flex-direction: column; gap: 6px; }
        .form-group label { font-size: 13px; color: #aaa; font-weight: bold; }
        
        input, select { background: #121212; color: white; border: 1px solid #444; padding: 10px; border-radius: 4px; font-size: 14px; width: 100%; box-sizing: border-box; }
        input:focus, select:focus { border-color: #00e5ff; outline: none; }
        
        /* Lista de Clientes Blindada para Telas Pequenas */
        .table-responsive { overflow-x: auto; background: #161616; border-radius: 6px; border: 1px solid #333; margin-top: 15px; }
        table { width: 100%; border-collapse: collapse; min-width: 900px; }
        th, td { border-bottom: 1px solid #2a2a2a; padding: 12px; text-align: left; font-size: 14px; }
        th { background-color: #242424; color: #00e5ff; font-weight: 600; text-transform: uppercase; font-size: 12px; letter-spacing: 0.5px; }
        tr:hover td { background-color: #1e1e1e; }
        
        .btn-submit { background-color: #00e5ff; color: #121212; border: none; padding: 12px 25px; cursor: pointer; font-weight: bold; border-radius: 4px; transition: 0.3s; font-size: 14px; width: auto; display: inline-block; }
        .btn-submit:hover { background-color: #00b4d8; }
        .btn-danger { background-color: #ff4d4d; color: white; border: none; padding: 6px 12px; cursor: pointer; border-radius: 4px; font-size: 12px; font-weight: bold; }
        .btn-danger:hover { background-color: #ff1a1a; }
        .btn-edit { background-color: #ff9e00; color: #121212; border: none; padding: 6px 12px; cursor: pointer; border-radius: 4px; font-size: 12px; font-weight: bold; text-decoration: none; }
        .btn-edit:hover { background-color: #e68a00; }
        .logout { background-color: #ff4d4d; color: white; text-decoration: none; padding: 8px 15px; border-radius: 4px; font-weight: bold; font-size: 13px; }
        .actions-cell { display: flex; gap: 8px; }
        .card-title { color: #ff9e00; margin-top: 0; margin-bottom: 18px; }
        .row-inline { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 10px; }
        .btn-secondary { background: #2a2a2a; color: #e0e0e0; border: 1px solid #444; padding: 10px 18px; border-radius: 4px; cursor: pointer; font-weight: bold; font-size: 13px; }
        .btn-secondary:hover { background: #333; }
        .status-msg { font-size: 13px; padding: 8px 12px; border-radius: 4px; margin-top: 10px; display: none; }
        .status-msg.ok { background: rgba(0,230,118,0.12); color: #00e676; display: block; }
        .status-msg.error { background: rgba(255,77,77,0.12); color: #ff6b6b; display: block; }
        .badge-engine { padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        .badge-engine.sqlite { background: rgba(255,158,0,0.15); color: #ff9e00; }
        .badge-engine.postgres { background: rgba(0,229,255,0.15); color: #00e5ff; }
        .badge-status { padding: 3px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; }
        .badge-status.ok { background: rgba(0,230,118,0.15); color: #00e676; }
        .badge-status.error { background: rgba(255,77,77,0.15); color: #ff6b6b; }
        .field-hint { font-size: 11px; color: #777; margin-top: 4px; }
        .checkbox-row { display: flex; align-items: center; gap: 8px; }
        .checkbox-row input { width: auto; }
        
        @media (max-width: 768px) {
            .header { flex-direction: column; align-items: flex-start; }
            .header div { width: 100%; display: flex; justify-content: space-between; margin-top: 5px; }
            .btn-submit { width: 100%; text-align: center; }
        }

        /* ===== Shell com barra lateral (estilo Zabbix) ===== */
        .admin-shell { display: flex; min-height: 100vh; }
        .admin-sidebar { width: 240px; flex-shrink: 0; background: #0b1117; border-right: 1px solid #262626; display: flex; flex-direction: column; position: fixed; top: 0; left: 0; height: 100vh; overflow-y: auto; z-index: 10; }
        .sidebar-brand { padding: 20px 18px; font-size: 17px; font-weight: bold; color: #00e5ff; border-bottom: 1px solid #1f2933; display: flex; align-items: center; gap: 8px; }
        .sidebar-nav { flex: 1; padding: 14px 10px; display: flex; flex-direction: column; gap: 3px; }
        .nav-item { display: flex; align-items: center; gap: 10px; padding: 11px 14px; border-radius: 6px; color: #9aa5b1; text-decoration: none; font-size: 14px; font-weight: 500; cursor: pointer; border: none; background: transparent; width: 100%; text-align: left; }
        .nav-item:hover { background: #161d26; color: #e0e0e0; }
        .nav-item.active { background: rgba(0,229,255,0.1); color: #00e5ff; font-weight: bold; }
        .sidebar-footer { padding: 14px 10px; border-top: 1px solid #1f2933; display: flex; flex-direction: column; gap: 3px; }
        .admin-content { margin-left: 240px; flex: 1; padding: 28px 32px; max-width: 1200px; }
        .admin-tab { display: none; }
        .admin-tab.active { display: block; }
        .content-title { color: #00e5ff; font-size: 22px; margin: 0 0 22px 0; }

        .pagination-bar { display: flex; align-items: center; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
        .pagination-bar .page-info { font-size: 13px; color: #888; }
        .search-box { max-width: 280px; }

        @media (max-width: 900px) {
            .admin-sidebar { position: relative; width: 100%; height: auto; }
            .admin-content { margin-left: 0; padding: 20px; }
            .admin-shell { flex-direction: column; }
        }

        /* Logs (aba) */
        #logBox { background: #05080b; border: 1px solid #1f2933; border-radius: 6px; padding: 15px; height: 60vh; overflow-y: auto; font-family: "SF Mono", Consolas, monospace; font-size: 12.5px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
        .l-error { color: #ff6b6b; }
        .l-warning { color: #ff9e00; }
        .l-info { color: #8ea3c4; }
        .l-default { color: #667; }
    </style>
</head>
<body>
    <div class="admin-shell">
        <aside class="admin-sidebar">
            <div class="sidebar-brand">🔮 Oráculo <span style="font-size:10px; background:#00e5ff; color:#0b1117; padding:2px 6px; border-radius:8px; font-weight:bold;">ADMIN</span></div>
            <nav class="sidebar-nav">
                <a href="#usuarios" class="nav-item" data-tab="usuarios">👤 Usuários</a>
                <a href="#banco" class="nav-item" data-tab="banco">🗄️ Banco de Dados</a>
                <a href="#ia" class="nav-item" data-tab="ia">🧠 Inteligência Artificial</a>
                <a href="#branding" class="nav-item" data-tab="branding">🖼️ Branding</a>
                <a href="#backup" class="nav-item" data-tab="backup">💾 Backup</a>
                <a href="#logs" class="nav-item" data-tab="logs">📜 Logs</a>
            </nav>
            <div class="sidebar-footer">
                <a href="/chat" class="nav-item">💬 Voltar ao Chat</a>
                <a href="/logout" class="nav-item" style="color:#ff4d4d;">🚪 Sair</a>
            </div>
        </aside>

        <main class="admin-content">
        <section id="tab-usuarios" class="admin-tab">
        <h2 class="content-title">👤 Usuários</h2>
        <div class="form-container">
            <h3 id="form-title" style="color: #ff9e00; margin-top: 0; margin-bottom: 20px;">Cadastrar / Atualizar Conta</h3>
            <form method="POST" action="/admin/save" id="userForm">
                <div class="form-grid">
                    <div class="form-group">
                        <label>Usuário (Login)</label>
                        <input type="text" name="username" id="form-username" placeholder="ex: pollux" required>
                    </div>
                    <div class="form-group">
                        <label>Senha</label>
                        <input type="text" name="password" id="form-password" placeholder="Senha (vazio p/ manter)">
                    </div>
                    <div class="form-group">
                        <label>Perfil de Acesso</label>
                        <select name="profile" id="form-profile">
                            <option value="CLIENTE">CLIENTE</option>
                            <option value="NOC">NOC</option>
                            <option value="ADMINISTRADOR">ADMINISTRADOR</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Grupo Zabbix</label>
                        <input type="text" name="grupo_cliente" id="form-grupo" placeholder="VDC_POLLUX" required>
                    </div>
                    <div class="form-group">
                        <label>Modelo de LLM (Oracle OCI)</label>
                        <input type="text" name="ai_model" id="form-model" placeholder="cohere.command-r-08-2024" required>
                    </div>
                    <div class="form-group">
                        <label>Zabbix URL (Específica)</label>
                        <input type="url" name="zabbix_url" id="form-url" placeholder="https://.../api_jsonrpc.php">
                    </div>
                    <div class="form-group">
                        <label>Token Zabbix</label>
                        <input type="text" name="token_zabbix" id="form-token" placeholder="Token da API">
                    </div>
                </div>
                <button type="submit" class="btn-submit" id="form-submit-btn">Salvar Configuração</button>
            </form>
        </div>

        <h3>Contas Cadastradas no Oráculo</h3>
        <div class="row-inline" style="margin-bottom: 4px;">
            <input type="text" id="users-search" class="search-box" placeholder="Buscar por usuário ou grupo Zabbix...">
        </div>
        <div class="table-responsive">
            <table>
                <thead>
                    <tr>
                        <th style="width: 50px;">ID</th>
                        <th>Usuário</th>
                        <th>Perfil</th>
                        <th>Grupo Zabbix</th>
                        <th>Modelo LLM</th>
                        <th>Zabbix URL Endpoint</th>
                        <th style="width: 150px;">Ações</th>
                    </tr>
                </thead>
                <tbody id="users-tbody">
                    <tr><td colspan="7" style="text-align:center; color:#666;">Carregando...</td></tr>
                </tbody>
            </table>
        </div>
        <div class="pagination-bar">
            <button type="button" class="btn-secondary" id="users-prev" onclick="mudarPaginaUsuarios(-1)">← Anterior</button>
            <span class="page-info" id="users-page-info"></span>
            <button type="button" class="btn-secondary" id="users-next" onclick="mudarPaginaUsuarios(1)">Próxima →</button>
        </div>
        </section>

        <section id="tab-banco" class="admin-tab">
        <h2 class="content-title">🗄️ Banco de Dados</h2>
        <div class="form-container">
            <p style="font-size:13px; color:#aaa; margin-top:-10px;">
                Atual: <span class="badge-engine {{ db_cfg.engine }}">{{ 'PostgreSQL' if db_cfg.engine == 'postgres' else 'SQLite (local)' }}</span>
                {% if db_cfg.engine == 'postgres' %}— {{ db_cfg.host }}:{{ db_cfg.port }}/{{ db_cfg.dbname }}{% endif %}
            </p>
            <div class="form-grid">
                <div class="form-group">
                    <label>Motor</label>
                    <select id="db-engine" onchange="toggleDbFields()">
                        <option value="sqlite" {{ 'selected' if db_cfg.engine == 'sqlite' else '' }}>SQLite (arquivo local)</option>
                        <option value="postgres" {{ 'selected' if db_cfg.engine == 'postgres' else '' }}>PostgreSQL</option>
                    </select>
                </div>
                <div class="form-group db-pg-field">
                    <label>Host</label>
                    <input type="text" id="db-host" placeholder="10.0.0.230" value="{{ db_cfg.host if db_cfg.engine == 'postgres' else '' }}">
                </div>
                <div class="form-group db-pg-field">
                    <label>Porta</label>
                    <input type="text" id="db-port" placeholder="5432" value="{{ db_cfg.port if db_cfg.engine == 'postgres' else 5432 }}">
                </div>
                <div class="form-group db-pg-field">
                    <label>Nome do Banco</label>
                    <input type="text" id="db-name" placeholder="oraculo" value="{{ db_cfg.dbname if db_cfg.engine == 'postgres' else '' }}">
                </div>
                <div class="form-group db-pg-field">
                    <label>Usuário</label>
                    <input type="text" id="db-user" placeholder="oraculo" value="{{ db_cfg.user if db_cfg.engine == 'postgres' else '' }}">
                </div>
                <div class="form-group db-pg-field">
                    <label>Senha</label>
                    <input type="password" id="db-password" placeholder="Deixe em branco p/ manter a atual">
                </div>
            </div>
            <p class="field-hint db-pg-field">O banco e o usuário já devem existir no servidor PostgreSQL — este painel não cria banco novo nem usuário novo, só testa a conexão e cria/atualiza as tabelas do Oráculo dentro do banco informado.</p>
            <div class="row-inline">
                <button type="button" class="btn-secondary" onclick="testarConexaoDb()">🔌 Testar Conexão</button>
                <button type="button" class="btn-submit" onclick="aplicarConfigDb()">💾 Salvar e Aplicar</button>
            </div>
            <div id="db-status-msg" class="status-msg"></div>
        </div>
        </section>

        <section id="tab-ia" class="admin-tab">
        <h2 class="content-title">🧠 Inteligência Artificial</h2>
        <div class="form-container">
            <p style="font-size:13px; color:#aaa; margin-top:-10px;">
                Atual: <span style="background:#00e5ff22; color:#00e5ff; padding:2px 8px; border-radius:8px; font-weight:bold;">{{ ia_labels.get(ia_cfg.provider, ia_cfg.provider) }}</span>
                {% if ia_cfg.model %}— modelo <code>{{ ia_cfg.model }}</code>{% endif %}
            </p>
            <div class="form-grid">
                <div class="form-group">
                    <label>Provedor</label>
                    <select id="ia-provider" onchange="toggleIaFields()">
                        <option value="oci" {{ 'selected' if ia_cfg.provider == 'oci' else '' }}>Oracle OCI GenAI (Cohere)</option>
                        <option value="openai" {{ 'selected' if ia_cfg.provider == 'openai' else '' }}>OpenAI (GPT)</option>
                        <option value="gemini" {{ 'selected' if ia_cfg.provider == 'gemini' else '' }}>Google Gemini</option>
                        <option value="anthropic" {{ 'selected' if ia_cfg.provider == 'anthropic' else '' }}>Anthropic (Claude)</option>
                        <option value="openai_compat" {{ 'selected' if ia_cfg.provider == 'openai_compat' else '' }}>OpenAI-compatível (Groq, DeepSeek, Ollama...)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Modelo <span style="color:#667;">(em branco = padrão do provedor)</span></label>
                    <input type="text" id="ia-model" list="ia-model-sugestoes" placeholder="ex.: gpt-4o-mini" value="{{ ia_cfg.model }}">
                    <datalist id="ia-model-sugestoes"></datalist>
                </div>
                <div class="form-group ia-key-field">
                    <label>API Key</label>
                    <input type="password" id="ia-api-key" placeholder="{{ 'Já salva — deixe em branco p/ manter (' + ia_cfg.api_key + ')' if ia_cfg.api_key else 'Cole a chave do provedor' }}">
                </div>
                <div class="form-group ia-baseurl-field">
                    <label>Base URL</label>
                    <input type="text" id="ia-base-url" placeholder="ex.: http://10.0.0.50:11434/v1 (Ollama)" value="{{ ia_cfg.base_url }}">
                </div>
            </div>
            <p class="field-hint" id="ia-hint"></p>
            <div class="row-inline">
                <button type="button" class="btn-secondary" onclick="testarIa()">🔌 Testar Conexão</button>
                <button type="button" class="btn-submit" onclick="aplicarIa()">💾 Salvar e Aplicar</button>
            </div>
            <div id="ia-status-msg" class="status-msg"></div>
        </div>
        </section>

        <section id="tab-branding" class="admin-tab">
        <h2 class="content-title">🖼️ Branding</h2>
        <div class="form-container">
            <p style="font-size:13px; color:#aaa; margin-top:-10px;">
                O logo enviado aqui aparece automaticamente no topo dos relatórios gerados
                (SecOps e VMs/FinOps). PNG, JPG, WEBP ou SVG — até 2 MB.
            </p>
            {% if logo.existe %}
            <div class="row-inline" style="align-items:flex-start; margin-top:14px;">
                <img src="{{ logo.data_uri }}" alt="Logo atual" style="max-height:70px; max-width:260px; object-fit:contain; background:#0b1117; border:1px solid #2d3748; border-radius:6px; padding:8px;">
                <div>
                    <p style="margin:0; font-size:13px; color:#00e676;">✅ Logo ativo — {{ logo.nome }} ({{ logo.tamanho_kb }} KB)</p>
                    <button type="button" class="btn-danger" style="margin-top:8px;" onclick="removerLogo()">🗑️ Remover Logo</button>
                </div>
            </div>
            {% else %}
            <p style="font-size:13px; color:#666; margin-top:14px;">Nenhum logo configurado ainda — os relatórios saem sem logo no topo.</p>
            {% endif %}
            <div class="row-inline" style="margin-top:16px;">
                <input type="file" id="logo-upload-file" accept=".png,.jpg,.jpeg,.webp,.svg">
                <button type="button" class="btn-submit" onclick="enviarLogo()">📤 Enviar Logo</button>
            </div>
            <div id="logo-status-msg" class="status-msg"></div>
        </div>
        </section>

        <section id="tab-backup" class="admin-tab">
        <h2 class="content-title">💾 Backup</h2>
        <div class="form-container">
            <div class="form-grid">
                <div class="form-group">
                    <div class="checkbox-row">
                        <input type="checkbox" id="bk-enabled" {{ 'checked' if backup_cfg.enabled else '' }}>
                        <label style="margin:0;">Backup automático ativado</label>
                    </div>
                </div>
                <div class="form-group">
                    <label>Frequência</label>
                    <select id="bk-frequency" onchange="toggleWeekdayField()">
                        <option value="daily" {{ 'selected' if backup_cfg.frequency == 'daily' else '' }}>Diário</option>
                        <option value="weekly" {{ 'selected' if backup_cfg.frequency == 'weekly' else '' }}>Semanal</option>
                    </select>
                </div>
                <div class="form-group" id="bk-weekday-field">
                    <label>Dia da semana</label>
                    <select id="bk-weekday">
                        <option value="0" {{ 'selected' if backup_cfg.weekday == 0 else '' }}>Segunda</option>
                        <option value="1" {{ 'selected' if backup_cfg.weekday == 1 else '' }}>Terça</option>
                        <option value="2" {{ 'selected' if backup_cfg.weekday == 2 else '' }}>Quarta</option>
                        <option value="3" {{ 'selected' if backup_cfg.weekday == 3 else '' }}>Quinta</option>
                        <option value="4" {{ 'selected' if backup_cfg.weekday == 4 else '' }}>Sexta</option>
                        <option value="5" {{ 'selected' if backup_cfg.weekday == 5 else '' }}>Sábado</option>
                        <option value="6" {{ 'selected' if backup_cfg.weekday == 6 else '' }}>Domingo</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Horário (servidor)</label>
                    <input type="text" id="bk-time" placeholder="02:00" value="{{ backup_cfg.time }}">
                </div>
                <div class="form-group">
                    <label>Reter últimos (nº de backups)</label>
                    <input type="text" id="bk-retention" placeholder="14" value="{{ backup_cfg.retention }}">
                </div>
            </div>
            <div class="row-inline">
                <button type="button" class="btn-submit" onclick="salvarConfigBackup()">💾 Salvar Agendamento</button>
                <button type="button" class="btn-secondary" onclick="rodarBackupAgora()">▶️ Rodar Backup Agora</button>
            </div>
            <div id="bk-status-msg" class="status-msg"></div>

            <h3 class="card-title" style="margin-top:30px; font-size:16px;">Restaurar de um Arquivo Enviado</h3>
            <p class="field-hint" style="margin-top:0;">Útil para repovoar uma instalação nova a partir de um backup baixado (arquivo .db do SQLite ou .sql/.sql.gz do PostgreSQL). <strong style="color:#ff6b6b;">Isso substitui TODOS os dados atuais.</strong></p>
            <div class="row-inline">
                <input type="file" id="bk-upload-file" accept=".db,.sql,.gz" style="max-width:320px;">
                <button type="button" class="btn-danger" onclick="restaurarUpload()">♻️ Restaurar Backup Enviado</button>
            </div>
            <div id="bk-upload-status-msg" class="status-msg"></div>

            <h3 class="card-title" style="margin-top:30px; font-size:16px;">Histórico de Backups</h3>
            <div class="table-responsive">
                <table>
                    <thead>
                        <tr>
                            <th>Data/Hora</th><th>Tipo</th><th>Motor</th><th>Status</th><th>Tamanho</th><th>Mensagem</th><th style="width:180px;">Ação</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for b in backups %}
                        <tr>
                            <td>{{ b.started_at }}</td>
                            <td>{{ '♻️ Restauração' if b.kind == 'restore' else '💾 Backup' }}</td>
                            <td>{{ b.engine }}</td>
                            <td><span class="badge-status {{ b.status }}">{{ 'OK' if b.status == 'ok' else 'ERRO' }}</span></td>
                            <td>{{ b.size_bytes | format_size }}</td>
                            <td style="max-width:260px; font-size:12px; color:#999;">{{ b.message }}</td>
                            <td class="actions-cell">
                                {% if b.status == 'ok' and b.kind != 'restore' %}
                                <a href="/admin/backup/download/{{ b.id }}" class="btn-edit" style="text-decoration:none;">Baixar</a>
                                <button type="button" class="btn-danger" onclick="restaurarBackup({{ b.id }}, '{{ b.started_at }}')">Restaurar</button>
                                {% endif %}
                            </td>
                        </tr>
                        {% else %}
                        <tr><td colspan="7" style="text-align:center; color:#666;">Nenhum backup executado ainda.</td></tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        </section>

        <section id="tab-logs" class="admin-tab">
        <h2 class="content-title">📜 Logs do Sistema</h2>
        <div class="form-container">
            <div class="row-inline">
                <select id="logs-file-select"></select>
                <select id="logs-lines-select">
                    <option value="200">200 linhas</option>
                    <option value="500" selected>500 linhas</option>
                    <option value="1500">1500 linhas</option>
                    <option value="5000">5000 linhas</option>
                </select>
                <input type="text" id="logs-filter-input" class="search-box" placeholder="Filtrar (ex: ERROR, username)">
                <label class="checkbox-row"><input type="checkbox" id="logs-auto-refresh" checked> Atualizar automaticamente</label>
                <button type="button" class="btn-submit" onclick="carregarLog()">🔄 Atualizar agora</button>
                <button type="button" class="btn-secondary" onclick="baixarLog()">⬇️ Baixar arquivo</button>
            </div>
            <p class="field-hint" id="logs-meta-info"></p>
            <div id="logBox">Selecione a aba para carregar...</div>
        </div>
        </section>
        </main>
    </div>

    <script>
        // ================== Navegação por abas (sidebar estilo Zabbix) ==================
        function mostrarAba(nome) {
            document.querySelectorAll('.admin-tab').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.nav-item[data-tab]').forEach(el => el.classList.remove('active'));
            const alvo = document.getElementById('tab-' + nome) ? nome : 'usuarios';
            document.getElementById('tab-' + alvo).classList.add('active');
            const navEl = document.querySelector('.nav-item[data-tab="' + alvo + '"]');
            if (navEl) navEl.classList.add('active');
            history.replaceState(null, '', '#' + alvo);
            if (alvo === 'usuarios') carregarUsuarios(1);
            if (alvo === 'logs') { entrarAbaLogs(); } else { pararAutoRefreshLogs(); }
        }
        document.querySelectorAll('.nav-item[data-tab]').forEach(el => {
            el.addEventListener('click', (ev) => { ev.preventDefault(); mostrarAba(el.dataset.tab); });
        });

        // ================== Usuários: busca + paginação (escala para milhares) ==================
        let usersPaginaAtual = 1;
        let usersTotalPaginas = 1;
        let usersBuscaTimer = null;

        function escapeHtml(s) {
            return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        async function carregarUsuarios(pagina) {
            usersPaginaAtual = pagina;
            const q = document.getElementById('users-search').value.trim();
            const tbody = document.getElementById('users-tbody');
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:#666;">Carregando...</td></tr>';
            try {
                const r = await fetch(`/admin/users/list?page=${pagina}&q=${encodeURIComponent(q)}`);
                const data = await r.json();
                if (!data.ok) { tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:#ff6b6b;">Erro ao carregar usuários.</td></tr>'; return; }

                usersTotalPaginas = data.total_pages;
                if (!data.users.length) {
                    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:#666;">Nenhum usuário encontrado.</td></tr>';
                } else {
                    tbody.innerHTML = data.users.map(u => `
                        <tr>
                            <td>${u.id}</td>
                            <td><strong>${escapeHtml(u.username)}</strong></td>
                            <td>${escapeHtml(u.profile)}</td>
                            <td><span style="color:#ff9e00; font-family: monospace;">${escapeHtml(u.grupo_cliente)}</span></td>
                            <td><span style="color:#00e5ff; font-family: monospace; font-size: 13px;">${escapeHtml(u.ai_model)}</span></td>
                            <td><span style="color:#00e676; font-size: 12px;">${u.zabbix_url ? escapeHtml(u.zabbix_url) : '🌍 Padrão Global (config.py)'}</span></td>
                            <td class="actions-cell">
                                <button type="button" class="btn-edit" onclick='editarUsuario(${JSON.stringify(u.username)}, ${JSON.stringify(u.profile)}, ${JSON.stringify(u.grupo_cliente)}, ${JSON.stringify(u.ai_model)}, ${JSON.stringify(u.zabbix_url || "")}, ${JSON.stringify(u.token_zabbix || "")})'>Editar</button>
                                <form method="POST" action="/admin/delete" style="margin: 0;" onsubmit="return confirm('Deseja excluir este usuário?');">
                                    <input type="hidden" name="id" value="${u.id}">
                                    <button type="submit" class="btn-danger">Excluir</button>
                                </form>
                            </td>
                        </tr>
                    `).join('');
                }

                document.getElementById('users-page-info').innerText = `Página ${data.page} de ${data.total_pages} (${data.total} usuário${data.total === 1 ? '' : 's'})`;
                document.getElementById('users-prev').disabled = data.page <= 1;
                document.getElementById('users-next').disabled = data.page >= data.total_pages;
            } catch (e) {
                tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:#ff6b6b;">Erro de comunicação.</td></tr>';
            }
        }
        function mudarPaginaUsuarios(delta) {
            const alvo = usersPaginaAtual + delta;
            if (alvo < 1 || alvo > usersTotalPaginas) return;
            carregarUsuarios(alvo);
        }
        document.getElementById('users-search').addEventListener('input', () => {
            clearTimeout(usersBuscaTimer);
            usersBuscaTimer = setTimeout(() => carregarUsuarios(1), 350);  // debounce
        });

        function editarUsuario(username, profile, grupo, model, url, token) {
            document.getElementById('form-title').innerHTML = "📝 Editando Conta: <strong style='color:#00e5ff;'>" + username + "</strong>";
            document.getElementById('form-username').value = username;
            document.getElementById('form-username').readOnly = true;
            document.getElementById('form-profile').value = profile;
            document.getElementById('form-grupo').value = grupo;
            document.getElementById('form-model').value = model || 'cohere.command-r-08-2024';
            document.getElementById('form-url').value = url;
            document.getElementById('form-token').value = token;
            document.getElementById('form-password').placeholder = "Deixe em branco p/ não alterar";
            document.getElementById('form-submit-btn').innerText = "Atualizar Conta";
            window.scrollTo({top: 0, behavior: 'smooth'});
        }

        // ---------- Banco de Dados ----------
        function toggleDbFields() {
            const isPg = document.getElementById('db-engine').value === 'postgres';
            document.querySelectorAll('.db-pg-field').forEach(el => el.style.display = isPg ? '' : 'none');
        }
        function coletarDbForm() {
            return {
                engine: document.getElementById('db-engine').value,
                host: document.getElementById('db-host').value.trim(),
                port: document.getElementById('db-port').value.trim() || '5432',
                dbname: document.getElementById('db-name').value.trim(),
                user: document.getElementById('db-user').value.trim(),
                password: document.getElementById('db-password').value,
            };
        }
        function mostrarStatus(elId, ok, msg) {
            const el = document.getElementById(elId);
            el.className = 'status-msg ' + (ok ? 'ok' : 'error');
            el.innerText = msg;
        }
        async function testarConexaoDb() {
            const body = coletarDbForm();
            mostrarStatus('db-status-msg', true, '🔄 Testando conexão...');
            try {
                const r = await fetch('/admin/db/test', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
                const data = await r.json();
                mostrarStatus('db-status-msg', data.ok, data.message);
            } catch (e) { mostrarStatus('db-status-msg', false, 'Erro de comunicação: ' + e); }
        }
        async function aplicarConfigDb() {
            const body = coletarDbForm();
            if (body.engine === 'postgres' && !confirm('Isso troca o banco de dados em uso AGORA MESMO para todos os usuários. Continuar?')) return;
            mostrarStatus('db-status-msg', true, '🔄 Aplicando e criando tabelas...');
            try {
                const r = await fetch('/admin/db/apply', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
                const data = await r.json();
                mostrarStatus('db-status-msg', data.ok, data.message);
            } catch (e) { mostrarStatus('db-status-msg', false, 'Erro de comunicação: ' + e); }
        }

        // ---------- Inteligência Artificial ----------
        const IA_SUGESTOES = {
            oci: ['cohere.command-r-08-2024', 'cohere.command-r-plus-08-2024'],
            openai: ['gpt-4o-mini', 'gpt-4o', 'gpt-4.1-mini'],
            gemini: ['gemini-2.0-flash', 'gemini-2.5-flash', 'gemini-2.5-pro'],
            anthropic: ['claude-sonnet-4-5', 'claude-haiku-4-5'],
            openai_compat: ['llama3.1', 'deepseek-chat', 'mistral-large-latest']
        };
        const IA_DICAS = {
            oci: 'A OCI autentica pelo arquivo ~/.oci/config do servidor (API key) ou por Instance Principals (VM na OCI) — o campo API Key não é usado.',
            openai: 'Crie a chave em platform.openai.com → API keys. Deixe a Base URL em branco.',
            gemini: 'Crie a chave gratuita em aistudio.google.com → Get API key.',
            anthropic: 'Crie a chave em console.anthropic.com → API keys.',
            openai_compat: 'Para Ollama local: Base URL http://IP:11434/v1, sem chave. Groq: https://api.groq.com/openai/v1 + chave. DeepSeek: https://api.deepseek.com + chave.'
        };
        function toggleIaFields() {
            const p = document.getElementById('ia-provider').value;
            document.querySelector('.ia-key-field').style.display = (p === 'oci') ? 'none' : '';
            document.querySelector('.ia-baseurl-field').style.display = (p === 'openai_compat') ? '' : 'none';
            document.getElementById('ia-hint').innerText = IA_DICAS[p] || '';
            const dl = document.getElementById('ia-model-sugestoes');
            dl.innerHTML = (IA_SUGESTOES[p] || []).map(m => `<option value="${m}">`).join('');
        }
        function coletarIaForm() {
            return {
                provider: document.getElementById('ia-provider').value,
                model: document.getElementById('ia-model').value.trim(),
                api_key: document.getElementById('ia-api-key').value.trim(),   // em branco = manter a salva
                base_url: document.getElementById('ia-base-url').value.trim()
            };
        }
        async function testarIa() {
            mostrarStatus('ia-status-msg', true, '🔄 Enviando uma pergunta de teste ao provedor...');
            try {
                const r = await fetch('/admin/ai/test', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(coletarIaForm()) });
                const data = await r.json();
                mostrarStatus('ia-status-msg', data.ok, data.message);
            } catch (e) { mostrarStatus('ia-status-msg', false, 'Erro de comunicação: ' + e); }
        }
        async function aplicarIa() {
            if (!confirm('Isso troca a IA em uso AGORA MESMO para todos os usuários do chat. Continuar?')) return;
            mostrarStatus('ia-status-msg', true, '🔄 Testando e aplicando...');
            try {
                const r = await fetch('/admin/ai/apply', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(coletarIaForm()) });
                const data = await r.json();
                mostrarStatus('ia-status-msg', data.ok, data.message);
                if (data.ok) setTimeout(() => window.location.reload(), 1500);
            } catch (e) { mostrarStatus('ia-status-msg', false, 'Erro de comunicação: ' + e); }
        }
        toggleIaFields();

        // ---------- Backup ----------
        function toggleWeekdayField() {
            document.getElementById('bk-weekday-field').style.display = document.getElementById('bk-frequency').value === 'weekly' ? '' : 'none';
        }
        async function salvarConfigBackup() {
            const body = {
                enabled: document.getElementById('bk-enabled').checked,
                frequency: document.getElementById('bk-frequency').value,
                weekday: parseInt(document.getElementById('bk-weekday').value, 10),
                time: document.getElementById('bk-time').value.trim() || '02:00',
                retention: parseInt(document.getElementById('bk-retention').value, 10) || 14,
            };
            try {
                const r = await fetch('/admin/backup/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
                const data = await r.json();
                mostrarStatus('bk-status-msg', data.ok, data.message);
            } catch (e) { mostrarStatus('bk-status-msg', false, 'Erro de comunicação: ' + e); }
        }
        async function rodarBackupAgora() {
            mostrarStatus('bk-status-msg', true, '🔄 Rodando backup agora, aguarde...');
            try {
                const r = await fetch('/admin/backup/run_now', { method: 'POST' });
                const data = await r.json();
                mostrarStatus('bk-status-msg', data.ok, data.message);
                if (data.ok) setTimeout(() => window.location.reload(), 1500);
            } catch (e) { mostrarStatus('bk-status-msg', false, 'Erro de comunicação: ' + e); }
        }

        function confirmarRestauracao(descricao) {
            const digitado = prompt(
                "⚠️ Atenção: isso vai apagar os dados atuais e substituir por " + descricao + ". " +
                "Um backup de segurança do estado atual será criado automaticamente antes. " +
                "Para confirmar, digite RESTAURAR (em maiúsculas) e clique OK:"
            );
            return digitado === "RESTAURAR";
        }
        async function restaurarBackup(id, dataHora) {
            if (!confirmarRestauracao("o backup de " + dataHora)) return;
            mostrarStatus('bk-status-msg', true, '🔄 Restaurando, isso pode levar alguns minutos...');
            try {
                const r = await fetch('/admin/backup/restore/' + id, { method: 'POST' });
                const data = await r.json();
                mostrarStatus('bk-status-msg', data.ok, data.message);
                if (data.ok) setTimeout(() => window.location.reload(), 2000);
            } catch (e) { mostrarStatus('bk-status-msg', false, 'Erro de comunicação: ' + e); }
        }
        async function restaurarUpload() {
            const fileInput = document.getElementById('bk-upload-file');
            const file = fileInput.files[0];
            if (!file) { mostrarStatus('bk-upload-status-msg', false, 'Selecione um arquivo primeiro.'); return; }
            if (!confirmarRestauracao("o arquivo enviado (" + file.name + ")")) return;
            mostrarStatus('bk-upload-status-msg', true, '🔄 Enviando e restaurando, isso pode levar alguns minutos...');
            const formData = new FormData();
            formData.append('backup_file', file);
            try {
                const r = await fetch('/admin/backup/restore_upload', { method: 'POST', body: formData });
                const data = await r.json();
                mostrarStatus('bk-upload-status-msg', data.ok, data.message);
                if (data.ok) setTimeout(() => window.location.reload(), 2000);
            } catch (e) { mostrarStatus('bk-upload-status-msg', false, 'Erro de comunicação: ' + e); }
        }

        // ---------- Branding (Logo) ----------
        async function enviarLogo() {
            const fileInput = document.getElementById('logo-upload-file');
            const file = fileInput.files[0];
            if (!file) { mostrarStatus('logo-status-msg', false, 'Selecione uma imagem primeiro.'); return; }
            mostrarStatus('logo-status-msg', true, '🔄 Enviando logo...');
            const formData = new FormData();
            formData.append('logo_file', file);
            try {
                const r = await fetch('/admin/branding/upload', { method: 'POST', body: formData });
                const data = await r.json();
                mostrarStatus('logo-status-msg', data.ok, data.message);
                if (data.ok) setTimeout(() => window.location.reload(), 1200);
            } catch (e) { mostrarStatus('logo-status-msg', false, 'Erro de comunicação: ' + e); }
        }
        async function removerLogo() {
            if (!confirm('Remover o logo atual? Os relatórios voltam a sair sem logo.')) return;
            try {
                const r = await fetch('/admin/branding/remove', { method: 'POST' });
                const data = await r.json();
                mostrarStatus('logo-status-msg', data.ok, data.message);
                if (data.ok) setTimeout(() => window.location.reload(), 800);
            } catch (e) { mostrarStatus('logo-status-msg', false, 'Erro de comunicação: ' + e); }
        }

        // ================== Logs (aba) ==================
        let logsArquivosCarregados = false;
        let logsAutoTimer = null;
        let logsUltimasLinhas = [];

        async function entrarAbaLogs() {
            if (!logsArquivosCarregados) {
                try {
                    const r = await fetch('/admin/logs/files');
                    const data = await r.json();
                    const sel = document.getElementById('logs-file-select');
                    if (data.ok && data.files.length) {
                        sel.innerHTML = data.files.map(f => `<option value="${f}">${f}</option>`).join('');
                        logsArquivosCarregados = true;
                    } else {
                        document.getElementById('logBox').innerHTML = '<div style="color:#556; text-align:center; padding:40px;">Nenhum arquivo .log encontrado.</div>';
                        return;
                    }
                } catch (e) {
                    document.getElementById('logBox').innerHTML = '<div style="color:#ff6b6b; text-align:center; padding:40px;">Erro ao listar logs.</div>';
                    return;
                }
            }
            carregarLog();
            reiniciarAutoRefreshLogs();
        }

        function corDaLinhaLog(linha) {
            if (/\|\s*ERROR\s*\|/.test(linha)) return 'l-error';
            if (/\|\s*WARNING\s*\|/.test(linha)) return 'l-warning';
            if (/\|\s*INFO\s*\|/.test(linha)) return 'l-info';
            return 'l-default';
        }
        function renderizarLog() {
            const filtro = document.getElementById('logs-filter-input').value.trim().toLowerCase();
            const box = document.getElementById('logBox');
            const linhasFiltradas = filtro ? logsUltimasLinhas.filter(l => l.toLowerCase().includes(filtro)) : logsUltimasLinhas;
            const estavaNoFim = (box.scrollTop + box.clientHeight) >= (box.scrollHeight - 40);
            box.innerHTML = linhasFiltradas.map(l => {
                const seguro = escapeHtml(l);
                return `<div class="${corDaLinhaLog(l)}">${seguro}</div>`;
            }).join('') || '<div style="color:#556; text-align:center; padding:20px;">Nenhuma linha corresponde ao filtro.</div>';
            if (estavaNoFim) box.scrollTop = box.scrollHeight;
        }
        async function carregarLog() {
            const file = document.getElementById('logs-file-select').value;
            if (!file) return;
            const lines = document.getElementById('logs-lines-select').value;
            try {
                const r = await fetch(`/admin/logs/tail?file=${encodeURIComponent(file)}&lines=${lines}`);
                const data = await r.json();
                if (!data.ok) { document.getElementById('logBox').innerHTML = '<div style="color:#ff6b6b;">' + escapeHtml(data.message) + '</div>'; return; }
                logsUltimasLinhas = data.lines || [];
                renderizarLog();
                const kb = (data.size / 1024).toFixed(1);
                const dt = new Date(data.mtime * 1000).toLocaleString('pt-BR');
                document.getElementById('logs-meta-info').innerText = `${data.file} — ${kb} KB — última modificação: ${dt}`;
            } catch (e) {
                document.getElementById('logBox').innerHTML = '<div style="color:#ff6b6b;">Erro ao carregar o log.</div>';
            }
        }
        function baixarLog() {
            const file = document.getElementById('logs-file-select').value;
            if (file) window.location.href = `/admin/logs/download/${encodeURIComponent(file)}`;
        }
        function reiniciarAutoRefreshLogs() {
            pararAutoRefreshLogs();
            if (document.getElementById('logs-auto-refresh').checked) {
                logsAutoTimer = setInterval(carregarLog, 4000);
            }
        }
        function pararAutoRefreshLogs() {
            if (logsAutoTimer) { clearInterval(logsAutoTimer); logsAutoTimer = null; }
        }
        document.getElementById('logs-file-select').addEventListener('change', carregarLog);
        document.getElementById('logs-lines-select').addEventListener('change', carregarLog);
        document.getElementById('logs-filter-input').addEventListener('input', renderizarLog);
        document.getElementById('logs-auto-refresh').addEventListener('change', reiniciarAutoRefreshLogs);

        toggleDbFields();
        toggleWeekdayField();
        mostrarAba((location.hash || '#usuarios').slice(1));
    </script>
</body>
</html>
"""

@bp.route('/', methods=['GET'])
@bp.route('/admin', methods=['GET'])
def index():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    # A tabela de usuários NÃO é mais embutida aqui (com milhares de contas,
    # renderizar tudo de uma vez travaria a página) — ela é carregada via
    # /admin/users/list, paginada, pelo JS assim que a aba "Usuários" abre.
    return render_template_string(
        HTML_TEMPLATE,
        db_cfg=db.current_config(),
        ia_cfg=ai_providers.current_config(),   # chave já vem MASCARADA daqui
        ia_labels=ai_providers.PROVIDER_LABELS,
        backup_cfg=backup_manager.load_config(),
        backups=backup_manager.historico(),
        logo=branding.logo_info(),
    )


@bp.route('/admin/users/list', methods=['GET'])
def users_list():
    guard = _admin_only_json()
    if guard: return guard

    try:
        page = max(1, int(request.args.get('page', 1) or 1))
    except (TypeError, ValueError):
        page = 1
    per_page = 25
    q = (request.args.get('q') or '').strip()

    where, params = "", []
    if q:
        where = "WHERE username LIKE ? OR grupo_cliente LIKE ?"
        params = [f"%{q}%", f"%{q}%"]

    total_row = db.query_one(f"SELECT COUNT(*) as c FROM users {where}", tuple(params))
    total = total_row['c'] if total_row else 0
    total_pages = max(1, -(-total // per_page))  # divisão "pra cima" sem importar math
    page = min(page, total_pages)
    offset = (page - 1) * per_page

    rows = db.query_all(
        f"SELECT id, username, profile, grupo_cliente, ai_model, zabbix_url, token_zabbix "
        f"FROM users {where} ORDER BY username ASC LIMIT ? OFFSET ?",
        tuple(params) + (per_page, offset)
    )
    return jsonify({
        "ok": True, "users": rows, "total": total,
        "page": page, "per_page": per_page, "total_pages": total_pages,
    })


@bp.route('/admin/save', methods=['POST'])
@bp.route('/save', methods=['POST'])
def save_user():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    username = request.form.get('username').strip()
    password = request.form.get('password')
    profile = request.form.get('profile')
    grupo_cliente = request.form.get('grupo_cliente').strip()
    token_zabbix = request.form.get('token_zabbix').strip()
    zabbix_url = request.form.get('zabbix_url').strip()
    ai_model = request.form.get('ai_model').strip()
    
    user = db.query_one("SELECT id FROM users WHERE username=?", (username,))
    
    if user:
        query = "UPDATE users SET profile=?, grupo_cliente=?, zabbix_url=?, ai_model=?"
        params = [profile, grupo_cliente, zabbix_url, ai_model]
        if password and password.strip() != "":
            query += ", password=?"
            params.append(generate_password_hash(password))  # nunca grava texto puro
        if token_zabbix:
            query += ", token_zabbix=?"
            params.append(token_zabbix)
        query += " WHERE username=?"
        params.append(username)
        db.execute(query, tuple(params))
    else:
        # Na criação: se vier senha, hasheia; se vier vazia, gera um hash aleatório
        # (conta fica sem login até definir a senha) — nunca armazena texto puro.
        if password and password.strip() != "":
            senha_hash = generate_password_hash(password)
        else:
            senha_hash = generate_password_hash(secrets.token_urlsafe(24))
        db.execute(
            "INSERT INTO users (username, password, profile, grupo_cliente, token_zabbix, zabbix_url, ai_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, senha_hash, profile, grupo_cliente, token_zabbix, zabbix_url, ai_model)
        )
    return redirect(request.referrer or url_for('admin.index'))

@bp.route('/admin/delete', methods=['POST'])
@bp.route('/delete', methods=['POST'])
def delete_user():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    user_id = request.form.get('id')
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    return redirect(request.referrer or url_for('admin.index'))


def _admin_only_json():
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return jsonify({"ok": False, "message": "Acesso negado."}), 403
    return None


@bp.route('/admin/db/test', methods=['POST'])
def db_test():
    guard = _admin_only_json()
    if guard: return guard
    data = request.json or {}
    engine = (data.get('engine') or 'sqlite').strip().lower()

    # Senha em branco = "usar a senha já salva" (o campo nunca é preenchido de
    # volta por segurança, então testar sem digitar de novo deve usar a atual
    # em vez de mandar senha vazia — era exatamente isso que causava o
    # "password authentication failed" ao clicar Testar Conexão sem retypar).
    password = data.get('password')
    if engine == 'postgres' and not password:
        password = db.current_config(mask_password=False).get('password') or ''

    ok, msg = db.test_connection(
        engine, host=data.get('host'), port=data.get('port'),
        dbname=data.get('dbname'), user=data.get('user'), password=password
    )
    return jsonify({"ok": ok, "message": msg})


@bp.route('/admin/db/apply', methods=['POST'])
def db_apply():
    guard = _admin_only_json()
    if guard: return guard
    data = request.json or {}
    engine = (data.get('engine') or 'sqlite').strip().lower()

    # Senha em branco = "manter a atual" (evita apagar a senha salva ao só trocar outro campo).
    password = data.get('password')
    if engine == 'postgres' and not password:
        password = db.current_config(mask_password=False).get('password') or ''

    if engine == 'postgres':
        ok, msg = db.test_connection(
            engine, host=data.get('host'), port=data.get('port'),
            dbname=data.get('dbname'), user=data.get('user'), password=password
        )
        if not ok:
            return jsonify({"ok": False, "message": f"Conexão falhou, nada foi alterado: {msg}"})

    try:
        db.reconfigure(
            engine, host=data.get('host'), port=data.get('port'),
            dbname=data.get('dbname'), user=data.get('user'), password=password, persist=True
        )
        db.init_schema()
        return jsonify({"ok": True, "message": "✅ Configuração aplicada e tabelas verificadas/criadas com sucesso."})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao aplicar/criar tabelas: {e}"})


@bp.route('/admin/ai/test', methods=['POST'])
def ai_test():
    guard = _admin_only_json()
    if guard: return guard
    data = request.json or {}

    # Chave em branco = "usar a chave já salva" (mesma regra da senha do banco:
    # o campo nunca é preenchido de volta por segurança, então testar sem
    # digitar de novo deve usar a atual em vez de mandar chave vazia).
    api_key = (data.get('api_key') or '').strip()
    if not api_key:
        api_key = ai_providers.current_config(mask_key=False).get('api_key') or ''

    ok, msg = ai_providers.test_connection(
        data.get('provider'), model=data.get('model'),
        api_key=api_key, base_url=data.get('base_url')
    )
    return jsonify({"ok": ok, "message": msg})


@bp.route('/admin/ai/apply', methods=['POST'])
def ai_apply():
    guard = _admin_only_json()
    if guard: return guard
    data = request.json or {}

    api_key = (data.get('api_key') or '').strip()
    if not api_key:
        api_key = ai_providers.current_config(mask_key=False).get('api_key') or ''

    # Testa ANTES de aplicar: uma IA mal configurada quebraria o chat de todo
    # mundo na hora — se o teste falhar, nada muda.
    ok, msg = ai_providers.test_connection(
        data.get('provider'), model=data.get('model'),
        api_key=api_key, base_url=data.get('base_url')
    )
    if not ok:
        return jsonify({"ok": False, "message": f"Nada foi alterado — {msg}"})

    try:
        ai_providers.reconfigure(
            data.get('provider'), model=data.get('model'),
            api_key=api_key, base_url=data.get('base_url'), persist=True
        )
        provedor = ai_providers.PROVIDER_LABELS.get((data.get('provider') or '').strip().lower(), '?')
        return jsonify({"ok": True, "message": f"✅ IA principal trocada para {provedor}. Vale imediatamente para as próximas mensagens."})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao aplicar: {e}"})


@bp.route('/admin/backup/config', methods=['POST'])
def backup_config_save():
    guard = _admin_only_json()
    if guard: return guard
    data = request.json or {}
    try:
        cfg = backup_manager.save_config({
            "enabled": bool(data.get('enabled')),
            "frequency": data.get('frequency') or 'daily',
            "weekday": int(data.get('weekday') or 0),
            "time": data.get('time') or '02:00',
            "retention": int(data.get('retention') or 14),
        })
        return jsonify({"ok": True, "message": "✅ Agendamento de backup salvo.", "config": cfg})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao salvar: {e}"})


@bp.route('/admin/backup/run_now', methods=['POST'])
def backup_run_now():
    guard = _admin_only_json()
    if guard: return guard
    resultado = backup_manager.run_backup_now()
    if resultado.get("status") == "ok":
        tamanho_fmt = format_size(resultado.get("size", 0))
        return jsonify({"ok": True, "message": f"✅ Backup concluído ({tamanho_fmt})."})
    return jsonify({"ok": False, "message": f"❌ {resultado.get('message', 'Falha desconhecida.')}"})


@bp.route('/admin/backup/download/<int:backup_id>', methods=['GET'])
def backup_download(backup_id):
    if session.get('profile') not in ['ADMIN', 'ADMINISTRADOR']:
        return "Acesso Negado.", 403
    row = backup_manager.get_backup(backup_id)
    if not row or row.get('status') != 'ok' or not row.get('file_path') or not os.path.exists(row['file_path']):
        return "Arquivo de backup não encontrado.", 404
    return send_file(row['file_path'], as_attachment=True)


@bp.route('/admin/backup/restore/<int:backup_id>', methods=['POST'])
def backup_restore(backup_id):
    guard = _admin_only_json()
    if guard: return guard
    row = backup_manager.get_backup(backup_id)
    if not row or row.get('status') != 'ok' or row.get('kind') != 'backup':
        return jsonify({"ok": False, "message": "Backup não encontrado no histórico."})
    if not row.get('file_path') or not os.path.exists(row['file_path']):
        return jsonify({"ok": False, "message": "Arquivo deste backup não existe mais em disco (pode ter sido removido pela retenção)."})

    resultado = backup_manager.restore_from_file(
        row['file_path'], f"backup #{row['id']} de {row['started_at']}", backup_engine_hint=row['engine']
    )
    return jsonify({"ok": resultado["status"] == "ok", "message": resultado["message"]})


@bp.route('/admin/backup/restore_upload', methods=['POST'])
def backup_restore_upload():
    guard = _admin_only_json()
    if guard: return guard
    f = request.files.get('backup_file')
    if not f or not f.filename:
        return jsonify({"ok": False, "message": "Nenhum arquivo enviado."})

    os.makedirs(backup_manager.BACKUP_DIR, exist_ok=True)
    nome_seguro = "".join(c for c in f.filename if c.isalnum() or c in "._-") or "upload"
    tmp_path = os.path.join(backup_manager.BACKUP_DIR, f"upload_tmp_{nome_seguro}")
    f.save(tmp_path)

    try:
        resultado = backup_manager.restore_from_file(tmp_path, f"upload: {f.filename}")
        return jsonify({"ok": resultado["status"] == "ok", "message": resultado["message"]})
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


@bp.route('/admin/branding/upload', methods=['POST'])
def branding_upload():
    guard = _admin_only_json()
    if guard: return guard
    f = request.files.get('logo_file')
    ok, msg = branding.salvar_logo(f)
    return jsonify({"ok": ok, "message": msg})


@bp.route('/admin/branding/remove', methods=['POST'])
def branding_remove():
    guard = _admin_only_json()
    if guard: return guard
    branding.remover_logo()
    return jsonify({"ok": True, "message": "Logo removido."})
