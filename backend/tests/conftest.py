"""Shared test fixtures.

`test_settings` builds a `Settings` instance with `_env_file=None` so tests never
depend on (or read) a developer's local `.env` — every field the tests need is
supplied explicitly here, including a throwaway JWT keypair and a throwaway
`LOCAL_MASTER_KEY` generated fresh per test (rather than fixed hardcoded secrets,
which gitleaks would flag).

`gemini_rpm_limit` is raised well above the production default. The limiter's window is keyed
by model and minute in one shared Redis database, so every test in a run competes for the same
60-request budget — a suite that grows past it starts failing tests that have nothing to do with
rate limiting. The limiter itself is exercised directly in `tests/unit`, not by accident here.
"""

import base64
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from relay_core.config import Settings

# The eval package is tested from here too (the integration suite drives its harness), so it is
# importable without `pip install -e ../evals/relay_eval` or a PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals" / "relay_eval"))


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
        gemini_rpm_limit=100_000,
        # Every test shares one Redis window; the production limits would trip the suite.
        rate_limit_messages_per_user_min=100_000,
        rate_limit_messages_per_workspace_min=100_000,
        rate_limit_connector_tests_min=100_000,
        local_master_key="base64:" + base64.b64encode(os.urandom(32)).decode(),
    )
