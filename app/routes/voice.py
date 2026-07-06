"""
V2 da assistente de voz (governança 100% OCI) — Caminho A: navegador direto.

Este blueprint expõe SOMENTE o endpoint que emite um token JWT de curta duração
para o SDK web da Oracle (@oracle/oci-ai-speech-realtime-web) conectar direto no
OCI Realtime Speech. O áudio vai do navegador para a sua tenancy OCI — não passa
por Google/OpenAI. O servidor autentica via INSTANCE PRINCIPAL (a identidade da
própria VM), sem chaves no código.

Pré-requisitos na OCI (ver guia que acompanha esta entrega):
  - Realtime Speech habilitado na tenancy/região (limited availability).
  - Dynamic Group da instância + policy permitindo 'ai-service-speech' no compartment.
  - Variáveis de ambiente: OCI_REGION e OCI_COMPARTMENT_ID.
  - pip install oci
"""
import os
import logging
from flask import Blueprint, jsonify, session
from app.routes.auth import login_required

logger = logging.getLogger('Oraculo')
bp = Blueprint('voice', __name__)

# V2 começa admin-only (como combinamos no V1). Para liberar a clientes depois,
# basta acrescentar perfis aqui.
PERFIS_VOZ = {"ADMIN", "ADMINISTRADOR"}


def _region():
    return (os.environ.get('OCI_REGION') or '').strip()


def _compartment():
    return (os.environ.get('OCI_COMPARTMENT_ID') or '').strip()


def _realtime_url(region):
    # Regra da Oracle: URL de speech com 'speech'->'realtime' e https->wss.
    return f"wss://realtime.aiservice.{region}.oci.oraclecloud.com"


@bp.route('/api/voice/token', methods=['POST'])
@login_required
def voice_token():
    perfil = str(session.get('profile') or '').upper().strip()
    if perfil not in PERFIS_VOZ:
        return jsonify({"error": "Acesso negado à voz OCI (restrito a administradores)."}), 403

    region = _region()
    compartment_id = _compartment()
    if not region or not compartment_id:
        return jsonify({"error": "Servidor sem OCI_REGION/OCI_COMPARTMENT_ID configurados."}), 500

    try:
        import oci
        # Instance Principal: usa a identidade da VM (nenhuma chave no código).
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        client = oci.ai_speech.AIServiceSpeechClient(config={'region': region}, signer=signer)

        details = oci.ai_speech.models.CreateRealtimeSessionTokenDetails(
            compartment_id=compartment_id
        )
        resp = client.create_realtime_session_token(details)
        data = resp.data

        return jsonify({
            "token": getattr(data, 'token', None),
            "sessionId": getattr(data, 'session_id', None),
            "compartmentId": compartment_id,
            "region": region,
            "realtimeUrl": _realtime_url(region),
            "language": (os.environ.get('OCI_STT_LANGUAGE') or 'pt-BR').strip()
        })
    except ImportError:
        logger.error("[Voice] SDK 'oci' não instalado no servidor.")
        return jsonify({"error": "SDK OCI não instalado (pip install oci)."}), 500
    except Exception as e:
        logger.error(f"[Voice] Falha ao gerar token OCI: {e}")
        return jsonify({"error": f"Falha ao gerar token OCI: {e}"}), 500
