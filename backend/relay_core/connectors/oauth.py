"""Connector OAuth: authorize, exchange, refresh (docs/system-design.md sections 15.3, 18.3).

Distinct from `relay_core.security.google_oauth`, which signs a *person* in (ADR-0007). This is
per-installation authorization: broader scopes, tokens encrypted into `connector_credentials`,
and a refresh cycle the workspace never sees.

Two HTTP calls with `httpx` rather than `google-auth-oauthlib`'s `Flow`: `Flow` is synchronous
(it carries `requests`), and it refuses a plain-`http` token endpoint without an environment
opt-out — which is exactly what the dev stack and the tests point it at. An authorization URL is
a query string and an exchange is a form POST, so the dependency buys nothing here.

Tokens are never logged. `refresh_if_expiring` is the only refresh implementation: the lazy path
(`relay_core.tools.sync.installation_secrets`, which every tool call goes through) and the beat
sweep (`relay_worker.tasks.connectors.refresh_oauth_tokens`) both land on it.
"""

import base64
import hashlib
import secrets as secrets_module
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings, get_settings
from relay_core.db.models.connectors import ConnectorCredential, ConnectorInstallation
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.security.credential_codec import encrypt_secrets
from relay_core.security.crypto import LocalKMS

_GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TIMEOUT_S = 15.0
# A token this close to expiry is refreshed before it reaches a connector, so a call can't start
# with a valid token and finish with an expired one.
_REFRESH_MARGIN_S = 60
# How far ahead the beat sweep works. Wider than the call path's margin because it runs on a
# schedule rather than at the moment a token is needed.
SWEEP_MARGIN_S = 15 * 60


class OAuthNotSupported(Exception):
    """The connector doesn't authorize through OAuth (postgres, mcp, openapi, web_search)."""


class InvalidGrant(Exception):
    """The refresh token is dead — consent withdrawn, or the grant expired. Only reconnecting
    fixes it, so this is the one token failure a caller must tell apart from a network blip."""


@dataclass(frozen=True)
class OAuthProvider:
    authorize_url: str
    scopes: list[str]


PROVIDERS: dict[str, OAuthProvider] = {
    "gmail": OAuthProvider(
        authorize_url=_GOOGLE_AUTHORIZE_URL,
        scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ],
    ),
    "google_calendar": OAuthProvider(
        authorize_url=_GOOGLE_AUTHORIZE_URL,
        scopes=[
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ],
    ),
}


def provider_for(connector_key: str) -> OAuthProvider:
    provider = PROVIDERS.get(connector_key)
    if provider is None:
        raise OAuthNotSupported(f"{connector_key!r} does not use OAuth")
    return provider


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scope: str


def redirect_uri(settings: Settings) -> str:
    """The one URI registered with the OAuth client. Everything else rides in `state`."""
    return f"{settings.api_base_url}/api/v1/oauth/callback"


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge). The verifier stays server-side; only the challenge travels."""
    verifier = secrets_module.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorization_url(
    provider: OAuthProvider, settings: Settings, *, state: str, code_challenge: str
) -> str:
    # `access_type=offline` with `prompt=consent` is what makes Google return a refresh token.
    # Without both, authorization comes back with an access token that dies in an hour and no
    # way to renew it.
    query = {
        "client_id": settings.google_oauth_client_id or "",
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{provider.authorize_url}?{urlencode(query)}"


async def exchange_code(settings: Settings, *, code: str, code_verifier: str) -> OAuthTokens:
    return await _post_token(
        settings.google_token_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri(settings),
            "client_id": settings.google_oauth_client_id or "",
            "client_secret": settings.google_oauth_client_secret or "",
        },
    )


def tokens_to_secrets(
    tokens: OAuthTokens, settings: Settings, previous: dict[str, str]
) -> dict[str, str]:
    """A refresh response usually omits `refresh_token`, so the stored one is carried forward.
    Losing it turns a working installation into one that needs reconnecting within the hour."""
    stored = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token or previous.get("refresh_token", ""),
        "token_uri": previous.get("token_uri") or settings.google_token_url,
        "scope": tokens.scope or previous.get("scope", ""),
    }
    return {key: value for key, value in stored.items() if value}


async def refresh_if_expiring(
    session: AsyncSession,
    kms: LocalKMS,
    installation: ConnectorInstallation,
    credential: ConnectorCredential,
    secrets: dict[str, str],
    settings: Settings | None = None,
    margin_s: int = _REFRESH_MARGIN_S,
) -> dict[str, str]:
    """The secrets a connector should use right now, refreshed first when the access token is
    about to expire. A non-OAuth credential passes straight through. `margin_s` is how far
    ahead of expiry to act: seconds on the call path, fifteen minutes for the beat sweep, which
    is the only difference between the two.

    Failures return what's stored rather than raising: a network blip must not take down a call
    that might still succeed, and a dead grant is recorded on the installation so an admin can
    see why the 401s started.
    """
    settings = settings or get_settings()
    refresh_token = secrets.get("refresh_token")
    expires_at = credential.oauth_expires_at
    if expires_at is None or not refresh_token:
        return secrets
    if expires_at > datetime.now(UTC) + timedelta(seconds=margin_s):
        return secrets

    try:
        tokens = await _post_token(
            secrets.get("token_uri") or settings.google_token_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.google_oauth_client_id or "",
                "client_secret": settings.google_oauth_client_secret or "",
            },
        )
    except InvalidGrant as exc:
        await ConnectorInstallationRepository(session).set_health(
            installation.workspace_id,
            installation.id,
            health="degraded",
            message=f"Google access needs reconnecting: {exc}",
        )
        return secrets
    except httpx.HTTPError:
        # Transient: the next call or the next sweep tries again. Marking an installation
        # degraded on a blip is noise an admin learns to ignore.
        return secrets

    refreshed = {**secrets, **tokens_to_secrets(tokens, settings, secrets)}
    await ConnectorCredentialRepository(session).put(
        workspace_id=installation.workspace_id,
        installation_id=installation.id,
        encrypted=encrypt_secrets(kms, refreshed),
        oauth_expires_at=tokens.expires_at,
    )
    return refreshed


async def _post_token(token_url: str, form: dict[str, str]) -> OAuthTokens:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as http:
        resp = await http.post(token_url, data=form)
    if resp.status_code >= 400:
        payload = _json_or_empty(resp)
        if payload.get("error") == "invalid_grant":
            raise InvalidGrant(str(payload.get("error_description", "invalid_grant")))
        raise httpx.HTTPStatusError(
            f"Token endpoint returned {resp.status_code}", request=resp.request, response=resp
        )
    payload = resp.json()
    return OAuthTokens(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_at=datetime.now(UTC) + timedelta(seconds=int(payload.get("expires_in", 3600))),
        scope=payload.get("scope", ""),
    )


def _json_or_empty(resp: httpx.Response) -> dict[str, object]:
    try:
        payload = resp.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
