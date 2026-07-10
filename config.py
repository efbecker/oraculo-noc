import os
import tempfile

# ================= CONFIGURAÇÕES GERAIS =================
SECRET_KEY = os.getenv("SECRET_KEY", "armazem_cloud_secret_key_noc_oraculo")
DB_PATH = '/opt/oraculo-noc/oraculo_zabbix.db'
AUDIO_DIR = os.path.join(tempfile.gettempdir(), 'oraculo_audio')

# Garante que a pasta de áudios temporários do TTS exista
os.makedirs(AUDIO_DIR, exist_ok=True)

# ================= CONFIGURAÇÕES DA OCI =================
OCI_ENDPOINT = "https://inference.generativeai.sa-saopaulo-1.oci.oraclecloud.com"
OCI_COMPARTMENT_ID = "ocid1.tenancy.oc1..aaaaaaaasavpc5ifi6ob3ygo63cdjuffblbyxucblyc7dx56k6a67m2fmyha"

# ================= CONFIGURAÇÕES DO ZABBIX =================
ZABBIX_URL = "https://monitoramento.armazem.cloud/api_jsonrpc.php"
