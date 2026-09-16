"""JSON <-> encrypted-bytes codec for connector secrets, on top of the generic envelope
encryption in `relay_core.security.crypto`. Kept separate so `crypto.py` stays a pure
bytes-in/bytes-out primitive, reusable for anything else that needs envelope encryption later.
"""

import json

from relay_core.db.models.connectors import ConnectorCredential
from relay_core.security.crypto import EncryptedSecrets, LocalKMS


def encrypt_secrets(kms: LocalKMS, secrets: dict[str, str]) -> EncryptedSecrets:
    return kms.encrypt(json.dumps(secrets).encode("utf-8"))


def decrypt_secrets(kms: LocalKMS, credential: ConnectorCredential) -> dict[str, str]:
    record = EncryptedSecrets(
        ciphertext=credential.ciphertext,
        nonce=credential.nonce,
        encrypted_dek=credential.encrypted_dek,
        kms_key_id=credential.kms_key_id,
    )
    decoded: dict[str, str] = json.loads(kms.decrypt(record).decode("utf-8"))
    return decoded
