import os
import sqlite3
from flask import Blueprint, request, jsonify, session, send_file
from werkzeug.security import generate_password_hash
from config import DB_PATH
from app.routes.auth import login_required
from datetime import datetime

bp = Blueprint('admin', __name__)

@bp.route("/api/settings", methods=["GET", "PUT"])
@login_required
def manage_settings():
    if session.get('role') != 'admin': return jsonify({"error": "Acesso negado."}), 403
    if request.method == "GET":
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            c = conn.cursor()
            c.execute("SELECT oci_model_id FROM settings WHERE id=1")
            row = c.fetchone()
            return jsonify({"oci_model_id": row[0] if row else ""})
    if request.method == "PUT":
        new_model = request.json.get("oci_model_id", "").strip()
        if not new_model: return jsonify({"error": "Vazio."}), 400
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.execute("UPDATE settings SET oci_model_id=? WHERE id=1", (new_model,))
            conn.commit()
        return jsonify({"status": "success"})

@bp.route("/api/users", methods=["GET", "POST"])
@login_required
def manage_users():
    if session.get('role') != 'admin': return jsonify({"error": "Acesso negado."}), 403
    
    if request.method == "GET":
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.row_factory = sqlite3.Row
            return jsonify([dict(r) for r in conn.execute("SELECT id, username, role, zabbix_token, client_group, ai_provider, ai_model FROM users").fetchall()])
            
    if request.method == "POST":
        data = request.json
        try:
            with sqlite3.connect(DB_PATH, timeout=15) as conn:
                conn.execute(
                    "INSERT INTO users (username, password, role, zabbix_token, client_group, ai_provider, ai_api_key, ai_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", 
                    (data['username'], generate_password_hash(data['password']), data['role'], data['zabbix_token'], data.get('client_group', 'GLOBAL'), data.get('ai_provider', 'oci'), data.get('ai_api_key', ''), data.get('ai_model', 'cohere.command-r-16k'))
                )
                conn.commit()
            return jsonify({"status": "success"})
        except Exception as e: 
            return jsonify({"error": str(e)}), 400

@bp.route("/api/users/<int:user_id>", methods=["PUT", "DELETE"])
@login_required
def update_delete_user(user_id):
    if session.get('role') != 'admin': return jsonify({"error": "Acesso negado."}), 403
    
    if request.method == "DELETE":
        if user_id == session.get('user_id'): return jsonify({"error": "Você não pode excluir a si mesmo."}), 400
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
        return jsonify({"status": "success"})

    if request.method == "PUT":
        data = request.json
        with sqlite3.connect(DB_PATH, timeout=15) as conn:
            fields = ["role = ?", "zabbix_token = ?", "client_group = ?", "ai_provider = ?", "ai_model = ?"]
            values = [data['role'], data.get('zabbix_token',''), data.get('client_group','GLOBAL'), data.get('ai_provider','oci'), data.get('ai_model','cohere.command-r-16k')]
            
            if data.get('password'):
                fields.append("password = ?")
                values.append(generate_password_hash(data['password']))
            if data.get('ai_api_key'):
                fields.append("ai_api_key = ?")
                values.append(data['ai_api_key'])
                
            values.append(user_id)
            conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", tuple(values))
            conn.commit()
        return jsonify({"status": "success"})

# ================= ROTA DE BACKUP ( DOWNLOAD DO BD ) =================
@bp.route("/api/admin/backup", methods=["GET"])
@login_required
def download_database():
    if session.get('role') != 'admin': 
        return "Acesso Negado. Apenas o NOC Admin pode exportar o banco de dados.", 403
        
    db_absolute_path = os.path.abspath(DB_PATH)
    data_atual = datetime.now().strftime('%Y-%m-%d_%H-%M')
    nome_arquivo = f"backup_oraculo_noc_{data_atual}.db"
    
    return send_file(
        db_absolute_path, 
        as_attachment=True, 
        download_name=nome_arquivo, 
        mimetype="application/octet-stream"
    )
