import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest

from relay_core.security.jwt import (
    InvalidAccessToken,
    create_access_token,
    decode_access_token,
)


def test_round_trip(test_settings) -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id, settings=test_settings)
    claims = decode_access_token(token, settings=test_settings)
    assert claims.sub == user_id
    assert claims.jti


def test_two_tokens_for_the_same_user_have_different_jti(test_settings) -> None:
    user_id = uuid.uuid4()
    token_a = create_access_token(user_id, settings=test_settings)
    token_b = create_access_token(user_id, settings=test_settings)
    assert (
        decode_access_token(token_a, settings=test_settings).jti
        != decode_access_token(token_b, settings=test_settings).jti
    )


def test_tampered_signature_is_rejected(test_settings) -> None:
    token = create_access_token(uuid.uuid4(), settings=test_settings)
    with pytest.raises(InvalidAccessToken):
        decode_access_token(token[:-1] + ("A" if token[-1] != "A" else "B"), settings=test_settings)


def test_expired_token_is_rejected(test_settings) -> None:
    # Build the payload directly rather than waiting out access_token_ttl_min.
    private_key = open(test_settings.jwt_private_key_path).read()
    now = datetime.now(UTC) - timedelta(minutes=20)
    token = pyjwt.encode(
        {"sub": str(uuid.uuid4()), "iat": now, "exp": now + timedelta(minutes=15), "jti": "x"},
        private_key,
        algorithm="EdDSA",
    )
    with pytest.raises(InvalidAccessToken):
        decode_access_token(token, settings=test_settings)


def test_wrong_key_is_rejected(test_settings, tmp_path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other_key = Ed25519PrivateKey.generate()
    token = pyjwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=15),
            "jti": "x",
        },
        other_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        algorithm="EdDSA",
    )
    with pytest.raises(InvalidAccessToken):
        decode_access_token(token, settings=test_settings)
