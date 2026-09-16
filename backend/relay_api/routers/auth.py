"""Auth routes (docs/system-design.md section 15.1; Google Sign-In per
docs/adr/0007-google-sign-in.md)."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import get_current_user, get_settings_dep
from relay_core.config import Settings
from relay_core.db.models.identity import User
from relay_core.db.repositories.refresh_tokens import RefreshTokenRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository
from relay_core.db.session import get_session
from relay_core.security import google_oauth
from relay_core.security.jwt import create_access_token
from relay_core.security.passwords import hash_password, verify_password
from relay_core.security.refresh_tokens import generate_refresh_token, hash_refresh_token

router = APIRouter(prefix="/auth", tags=["auth"])

_REFRESH_COOKIE = "relay_refresh"
_REFRESH_COOKIE_PATH = "/api/v1/auth"
_OAUTH_STATE_COOKIE = "relay_oauth_state"


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = Field(min_length=1)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class WorkspaceMembershipOut(BaseModel):
    workspace_id: uuid.UUID
    name: str
    slug: str
    role: str


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    workspaces: list[WorkspaceMembershipOut]


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        _REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_ttl_days * 86400,
        httponly=True,
        secure=settings.env != "dev",
        samesite="lax",
        path=_REFRESH_COOKIE_PATH,
    )


async def _issue_tokens(
    user: User,
    session: AsyncSession,
    settings: Settings,
    response: Response,
    *,
    family_id: uuid.UUID | None = None,
) -> AccessTokenResponse:
    family_id = family_id or uuid.uuid4()
    refresh_token = generate_refresh_token()
    await RefreshTokenRepository(session).create(
        user_id=user.id,
        token_hash=hash_refresh_token(refresh_token),
        family_id=family_id,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
    )
    _set_refresh_cookie(response, refresh_token, settings)
    access_token = create_access_token(user.id, settings=settings)
    return AccessTokenResponse(
        access_token=access_token, expires_in=settings.access_token_ttl_min * 60
    )


@router.post("/register", response_model=AccessTokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> AccessTokenResponse:
    users = UserRepository(session)
    if await users.get_by_email(body.email) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    user = await users.create(
        email=body.email, full_name=body.full_name, password_hash=hash_password(body.password)
    )
    return await _issue_tokens(user, session, settings, response)


@router.post("/login", response_model=AccessTokenResponse)
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> AccessTokenResponse:
    users = UserRepository(session)
    user = await users.get_by_email(body.email)
    if (
        user is None
        or user.password_hash is None
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account is disabled")
    user.last_login_at = datetime.now(UTC)
    return await _issue_tokens(user, session, settings, response)


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> AccessTokenResponse:
    token = request.cookies.get(_REFRESH_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing refresh token")

    repo = RefreshTokenRepository(session)
    existing = await repo.get_by_hash(hash_refresh_token(token))
    now = datetime.now(UTC)
    if existing is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    if existing.revoked_at is not None:
        # This token was already rotated (or explicitly revoked) and is being
        # presented again — reuse detection: treat the whole family as
        # compromised (docs/system-design.md section 18.2).
        await repo.revoke_family(existing.family_id, when=now)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Refresh token reuse detected; all sessions revoked"
        )
    if existing.expires_at < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token expired")

    await repo.revoke(existing, when=now)
    user = await UserRepository(session).get(existing.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account no longer available")

    return await _issue_tokens(user, session, settings, response, family_id=existing.family_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> None:
    token = request.cookies.get(_REFRESH_COOKIE)
    if token:
        repo = RefreshTokenRepository(session)
        existing = await repo.get_by_hash(hash_refresh_token(token))
        if existing is not None and existing.revoked_at is None:
            await repo.revoke(existing, when=datetime.now(UTC))
    response.delete_cookie(_REFRESH_COOKIE, path=_REFRESH_COOKIE_PATH)


@router.get("/me", response_model=MeResponse)
async def me(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    memberships = await WorkspaceMemberRepository(session).list_workspaces_for_user(user.id)
    return MeResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        workspaces=[
            WorkspaceMembershipOut(workspace_id=w.id, name=w.name, slug=w.slug, role=m.role)
            for m, w in memberships
        ],
    )


@router.get("/google/start")
async def google_start(settings: Settings = Depends(get_settings_dep)) -> RedirectResponse:
    try:
        url, state = google_oauth.authorization_url(settings)
    except google_oauth.GoogleSignInNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    redirect = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    redirect.set_cookie(
        _OAUTH_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=settings.env != "dev",
        samesite="lax",
        path="/api/v1/auth/google",
    )
    return redirect


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str,
    state: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> RedirectResponse:
    expected_state = request.cookies.get(_OAUTH_STATE_COOKIE)
    if not expected_state or expected_state != state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired OAuth state")

    try:
        identity = google_oauth.exchange_code(settings, code=code)
    except google_oauth.GoogleSignInNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    users = UserRepository(session)
    user = await users.get_by_google_sub(identity.sub)
    if user is None:
        existing = await users.get_by_email(identity.email)
        if existing is not None:
            # Auto-link only if both sides already vouch for the email
            # (docs/adr/0007-google-sign-in.md). Phase 1 has no email-verification
            # flow for password accounts, so `existing.email_verified` is only ever
            # true if it was set some other way — in practice this almost always
            # falls through to the conflict below, which is the ADR's intended
            # "otherwise require explicit confirmation" fallback.
            if existing.email_verified and identity.email_verified:
                existing.google_sub = identity.sub
                user = existing
            else:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "An account with this email already exists. Log in with your "
                    "password, then link Google from account settings.",
                )
        else:
            user = await users.create(
                email=identity.email,
                full_name=identity.full_name,
                google_sub=identity.sub,
                auth_provider="google",
                email_verified=identity.email_verified,
            )
    user.last_login_at = datetime.now(UTC)

    response = RedirectResponse(
        f"{settings.app_base_url}/login?oauth=success", status_code=status.HTTP_302_FOUND
    )
    await _issue_tokens(user, session, settings, response)
    response.delete_cookie(_OAUTH_STATE_COOKIE, path="/api/v1/auth/google")
    return response
