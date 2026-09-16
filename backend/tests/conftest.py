"""Shared test fixtures.

`test_settings` builds a `Settings` instance with `_env_file=None` so tests never
depend on (or read) a developer's local `.env` — every field the tests need is
supplied explicitly here, including a throwaway JWT keypair generated fresh per
test (rather than a fixed hardcoded key, which gitleaks would flag as a secret).
"""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from relay_core.config import Settings


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    private_key_path = tmp_path / "jwt_ed25519.pem"
    public_key_path = tmp_path / "jwt_ed25519.pub"
    private_key_path.write_bytes(private_pem)
    public_key_path.write_bytes(public_pem)

    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://relay:relay@localhost:5432/relay_test",
        langgraph_db_url="postgresql://relay:relay@localhost:5432/relay_test",
        redis_url="redis://localhost:6379/1",
        jwt_private_key_path=str(private_key_path),
        jwt_public_key_path=str(public_key_path),
        access_token_ttl_min=15,
        refresh_token_ttl_days=14,
    )
