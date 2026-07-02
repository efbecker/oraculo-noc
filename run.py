from app import create_app
from waitress import serve

app = create_app()

if __name__ == "__main__":
    print("🚀 Oráculo iniciado na porta 5000!")
    # O host 0.0.0.0 permite conexões locais de qualquer interface
    serve(app, host="0.0.0.0", port=5000)
