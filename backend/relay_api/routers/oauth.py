"""Connector OAuth routes (docs/system-design.md section 15.3, Phase 7 A3).

Two routes with very different trust levels. `oauth/start` is an ordinary admin-only workspace
route. `/oauth/callback` is called by Google in the user's browser, so it carries no session at
all — everything it is allowed to act on has to come from the signed `state` parameter it was
handed, and a `state` that doesn't verify is refused before anything is written.

The PKCE verifier never travels: `start` keeps it in Redis under a nonce that only the signed
state names, and the callback consumes it exactly once.
"""

import secrets as secrets_module
import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import CurrentUser, get_kms, get_redis, get_settings_dep, require_workspace_role
from relay_core.config import Settings
from relay_core.connectors.oauth import (
    OAuthNotSupported,
    authorization_url,
    exchange_code,
    pkce_pair,
    provider_for,
    tokens_to_secrets,
)
from relay_core.connectors.registry import CONNECTOR_TYPES
from relay_core.db.repositories.connector_credentials import ConnectorCredentialRepository
from relay_core.db.repositories.connector_installations import ConnectorInstallationRepository
from relay_core.db.repositories.workspaces import WorkspaceRepository
from relay_core.db.session import get_session
from relay_core.security.credential_codec import encrypt_secrets
from relay_core.security.crypto import LocalKMS
from relay_core.security.jwt import InvalidStateToken, create_state_token, decode_state_token
from relay_core.security.rbac import Role
from relay_core.tools.sync import installation_context, sync_installation

router = APIRouter(prefix="/workspaces/{workspace_id}/connectors", tags=["connectors"])
callback_router = APIRouter(prefix="/oauth", tags=["connectors"])

_VERIFIER_TTL_S = 600


def _verifier_key(nonce: str) -> str:
    return f"oauth:pkce:{nonce}"


class AuthorizeOut(BaseModel):
    authorize_url: str


@router.get("/{installation_id}/oauth/start", response_model=AuthorizeOut)
async def start_oauth(
    workspace_id: uuid.UUID = Path(...),
    installation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.admin.name)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> AuthorizeOut:
    installation = await ConnectorInstallationRepository(session).get(workspace_id, installation_id)
    if installation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connector installation not found")
    try:
        provider = provider_for(installation.connector_key)
    except OAuthNotSupported as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    verifier, challenge = pkce_pair()
    nonce = secrets_module.token_urlsafe(16)
    await redis.set(_verifier_key(nonce), verifier, ex=_VERIFIER_TTL_S)
    state = create_state_token(
        {
            "workspace_id": str(workspace_id),
            "installation_id": str(installation_id),
            "user_id": str(current.user.id),
            "nonce": nonce,
        },
        ttl_s=_VERIFIER_TTL_S,
        settings=settings,
    )
    return AuthorizeOut(
        authorize_url=authorization_url(provider, settings, state=state, code_challenge=challenge)
    )


@callback_router.get("/callback")
async def oauth_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    kms: LocalKMS = Depends(get_kms),
    settings: Settings = Depends(get_settings_dep),
) -> RedirectResponse:
    try:
        claims = decode_state_token(state, settings=settings)
    except InvalidStateToken as exc:
        # Nothing here is trustworthy — not even the installation to redirect back to — so this
        # ends in an error rather than a redirect.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid OAuth state: {exc}") from exc

    workspace_id = uuid.UUID(claims["workspace_id"])
    installation_id = uuid.UUID(claims["installation_id"])
    installations = ConnectorInstallationRepository(session)
    installation = await installations.get(workspace_id, installation_id)
    if installation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connector installation not found")

    workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None  # an installation cannot outlive its workspace (ON DELETE CASCADE)
    landing = f"{settings.app_base_url}/w/{workspace.slug}/connectors/{installation_id}"

    verifier = await redis.getdel(_verifier_key(claims["nonce"]))
    if error or code is None or verifier is None:
        # A denied consent screen, or a callback replayed after its verifier was used or expired.
        reason = error or ("expired" if verifier is None else "missing_code")
        return RedirectResponse(f"{landing}?error={reason}", status_code=status.HTTP_303_SEE_OTHER)

    try:
        tokens = await exchange_code(
            settings,
            code=code,
            code_verifier=verifier.decode() if isinstance(verifier, bytes) else str(verifier),
        )
    except Exception:  # noqa: BLE001 - the admin gets an error banner, not a 500 from Google
        return RedirectResponse(f"{landing}?error=exchange_failed", status_code=303)

    secrets = tokens_to_secrets(tokens, settings, previous={})
    await ConnectorCredentialRepository(session).put(
        workspace_id=workspace_id,
        installation_id=installation_id,
        encrypted=encrypt_secrets(kms, secrets),
        oauth_expires_at=tokens.expires_at,
    )

    connector = CONNECTOR_TYPES[installation.connector_key]()
    ctx = installation_context(workspace_id, uuid.UUID(claims["user_id"]), installation, secrets)
    try:
        healthy, message = await connector.health_check(ctx)
    except Exception as exc:  # noqa: BLE001 - a connected-but-unhealthy install must say why
        healthy, message = False, f"Health check raised: {exc}"
    await installations.set_health(
        workspace_id,
        installation_id,
        health="healthy" if healthy else "down",
        message=message,
        status="active" if healthy else "error",
    )
    if healthy:
        # The tools only become bindable once there's a token to call them with, so discovery
        # belongs here rather than at install time.
        await sync_installation(session, kms, installation, settings=settings)
    return RedirectResponse(f"{landing}?connected=1", status_code=status.HTTP_303_SEE_OTHER)
