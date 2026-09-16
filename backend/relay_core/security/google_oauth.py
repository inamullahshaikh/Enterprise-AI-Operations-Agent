"""Sign in with Google (docs/adr/0007-google-sign-in.md).

Requests only `openid email profile` — identity, nothing else. This is a distinct
flow from the Gmail/Calendar *connector* OAuth (installed per workspace, broader
scopes, tokens stored in `connector_credentials`); the two share only the same
Google Cloud OAuth client id/secret.
"""

from dataclasses import dataclass
from typing import Any

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow  # type: ignore[import-untyped]

from relay_core.config import Settings

_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


class GoogleSignInNotConfigured(Exception):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    email_verified: bool
    full_name: str


def _redirect_uri(settings: Settings) -> str:
    return f"{settings.api_base_url}/api/v1/auth/google/callback"


def build_flow(settings: Settings) -> Flow:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise GoogleSignInNotConfigured(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set"
        )
    client_config = {
        "web": {
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(
        client_config, scopes=_SCOPES, redirect_uri=_redirect_uri(settings)
    )


def authorization_url(settings: Settings) -> tuple[str, str]:
    """Returns (url, state). The caller must persist `state` (e.g. in a short-lived
    signed cookie) and check it against the callback's `state` query param."""
    flow = build_flow(settings)
    url, state = flow.authorization_url(
        access_type="online", include_granted_scopes="true", prompt="select_account"
    )
    return url, state


def exchange_code(settings: Settings, *, code: str) -> GoogleIdentity:
    flow = build_flow(settings)
    flow.fetch_token(code=code)
    credentials = flow.credentials
    claims: dict[str, Any] = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
        credentials.id_token, GoogleAuthRequest(), audience=settings.google_oauth_client_id
    )
    return GoogleIdentity(
        sub=claims["sub"],
        email=claims["email"],
        email_verified=bool(claims.get("email_verified", False)),
        full_name=claims.get("name", claims["email"]),
    )
