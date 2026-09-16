"""Envelope encryption for connector credentials (docs/system-design.md section 14.3's
`connector_credentials` columns, section 18 "encrypted credentials"). Every installation's
secrets are encrypted under a fresh, random data-encryption key (DEK); the DEK itself is
"wrapped" (encrypted) by a master key that never touches the database. Two ciphertexts
travel together, so compromising the database alone (without the master key) reveals nothing.

`LocalKMS` is the only provider implemented. `settings.kms_provider == "aws"` is declared in
`Settings` for forward compatibility with the section 23 deployment, but wiring up real AWS
KMS envelope encryption is a Phase 9 deploy concern — `build_kms` raises rather than silently
falling back to it.
"""

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from relay_core.config import Settings

_NONCE_LEN = 12
_LOCAL_KMS_KEY_ID = "local"


class KMSNotConfigured(Exception):
    pass


@dataclass(frozen=True)
class EncryptedSecrets:
    ciphertext: bytes
    nonce: bytes
    encrypted_dek: bytes
    kms_key_id: str


class LocalKMS:
    """`master_key` is 32 raw bytes (AES-256). Each `encrypt()` call generates a fresh DEK
    *and* a fresh nonce to wrap it with — the wrap nonce is prepended to `encrypted_dek`
    rather than given its own column, since `connector_credentials` only has one `nonce`
    column (used for the content ciphertext). Reusing a nonce across different DEKs under
    the same long-lived master key would break AES-GCM's guarantees, so this must never be
    a fixed nonce despite each individual DEK being used exactly once.
    """

    def __init__(self, master_key: bytes) -> None:
        self._master_key = master_key

    @classmethod
    def from_settings_value(cls, raw: str | None) -> "LocalKMS":
        if not raw or not raw.startswith("base64:"):
            raise KMSNotConfigured(
                "LOCAL_MASTER_KEY must be set to 'base64:<32 bytes>' when KMS_PROVIDER=local"
            )
        key = base64.b64decode(raw.removeprefix("base64:"))
        if len(key) != 32:
            raise KMSNotConfigured("LOCAL_MASTER_KEY must decode to exactly 32 bytes")
        return cls(key)

    def encrypt(self, plaintext: bytes) -> EncryptedSecrets:
        dek = AESGCM.generate_key(bit_length=256)
        content_nonce = os.urandom(_NONCE_LEN)
        ciphertext = AESGCM(dek).encrypt(content_nonce, plaintext, None)
        wrap_nonce = os.urandom(_NONCE_LEN)
        wrapped_dek = AESGCM(self._master_key).encrypt(wrap_nonce, dek, None)
        return EncryptedSecrets(
            ciphertext=ciphertext,
            nonce=content_nonce,
            encrypted_dek=wrap_nonce + wrapped_dek,
            kms_key_id=_LOCAL_KMS_KEY_ID,
        )

    def decrypt(self, record: EncryptedSecrets) -> bytes:
        if record.kms_key_id != _LOCAL_KMS_KEY_ID:
            raise KMSNotConfigured(f"Unknown kms_key_id {record.kms_key_id!r} for LocalKMS")
        wrap_nonce, wrapped_dek = (
            record.encrypted_dek[:_NONCE_LEN],
            record.encrypted_dek[_NONCE_LEN:],
        )
        dek = AESGCM(self._master_key).decrypt(wrap_nonce, wrapped_dek, None)
        return AESGCM(dek).decrypt(record.nonce, record.ciphertext, None)


def build_kms(settings: Settings) -> LocalKMS:
    if settings.kms_provider != "local":
        raise NotImplementedError(
            f"kms_provider={settings.kms_provider!r} is not implemented; AWS KMS envelope "
            "encryption is a Phase 9 deploy concern (docs/system-design.md section 23)"
        )
    return LocalKMS.from_settings_value(settings.local_master_key)
