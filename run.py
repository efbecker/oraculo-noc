from app import create_app
from waitress import serve

app = create_app()

if __name__ == "__main__":
    print("🚀 Oráculo iniciado na porta 5000!")
    # O host 0.0.0.0 permite conexões locais de qualquer interface.
    # threads=16: o padrão do waitress são só 4 threads — durante um relatório
    # pesado (145 hosts), o processamento em background disputa o GIL e as 4
    # threads ficavam sufocadas: o chat, os logs e o polling de progresso
    # paravam de responder e o Cloudflare estourava em 522. Com 16 threads,
    # sempre sobra thread livre para as requisições curtas da interface.
    # channel_timeout=120: derruba conexões penduradas em vez de acumular.
    serve(app, host="0.0.0.0", port=5000, threads=16, channel_timeout=120)
