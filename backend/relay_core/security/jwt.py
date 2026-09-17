"""Access token issuance/verification (docs/system-design.md section 18.2).

JWT access tokens are EdDSA-signed, live 15 minutes, and carry only `sub`
(user id), `iat`, `exp`, and `jti` — no roles. Workspace roles are looked up from
`workspace_members` on every request instead, so revoking a member's access takes
effect immediately rather than waiting out a token's lifetime.

`create_state_token`/`decode_state_token` sign short-lived OAuth `state` parameters
(Phase 7 A3) with the same key rather than introducing a second signing scheme. They
carry a `typ` claim, and each decoder checks it, so an access token can never be
replayed as a state parameter or the other way round.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt

from relay_core.config import Settings, get_settings


class InvalidAccessToken(Exception):
    pass


class InvalidStateToken(Exception):
    pass


@dataclass(frozen=True)
class AccessTokenClaims:
    sub: uuid.UUID
    jti: str
    issued_at: datetime
    expires_at: datetime


@lru_cache
def _load_private_key(path: str) -> str:
    with open(path, encoding="ascii") as f:
        return f.read()


@lru_cache
def _load_public_key(path: str) -> str:
    with open(path, encoding="ascii") as f:
        return f.read()


def create_access_token(user_id: uuid.UUID, *, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_min),
        "jti": str(uuid.uuid4()),
    }
    private_key = _load_private_key(settings.jwt_private_key_path)
    return jwt.encode(payload, private_key, algorithm="EdDSA")


def decode_access_token(token: str, *, settings: Settings | None = None) -> AccessTokenClaims:
    settings = settings or get_settings()
    public_key = _load_public_key(settings.jwt_public_key_path)
    try:
        payload = jwt.decode(token, public_key, algorithms=["EdDSA"])
    except jwt.PyJWTError as exc:
        raise InvalidAccessToken(str(exc)) from exc

    try:
        return AccessTokenClaims(
            sub=uuid.UUID(payload["sub"]),
            jti=payload["jti"],
            issued_at=datetime.fromtimestamp(payload["iat"], tz=UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidAccessToken("malformed claims") from exc


def create_state_token(
    claims: dict[str, str], *, ttl_s: int = 600, settings: Settings | None = None
) -> str:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    payload = {**claims, "typ": "oauth_state", "iat": now, "exp": now + timedelta(seconds=ttl_s)}
    return jwt.encode(payload, _load_private_key(settings.jwt_private_key_path), algorithm="EdDSA")


def decode_state_token(token: str, *, settings: Settings | None = None) -> dict[str, str]:
    settings = settings or get_settings()
    try:
        payload = jwt.decode(
            token, _load_public_key(settings.jwt_public_key_path), algorithms=["EdDSA"]
        )
    except jwt.PyJWTError as exc:
        raise InvalidStateToken(str(exc)) from exc
    if payload.get("typ") != "oauth_state":
        raise InvalidStateToken("not a state token")
    return payload
