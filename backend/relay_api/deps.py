"""Shared FastAPI dependencies: settings/session wiring, auth, and workspace
membership/RBAC (docs/system-design.md section 28, "Phase 1 - Foundation").
"""

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import cast

from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google import genai
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings, get_settings
from relay_core.db.models.identity import User, WorkspaceMember
from relay_core.db.repositories.llm_calls import LLMCallRepository, ModelPricingRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository
from relay_core.db.session import get_session
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.ratelimit import RedisRateLimiter
from relay_core.security.jwt import InvalidAccessToken, decode_access_token
from relay_core.security.rbac import has_at_least

__all__ = [
    "CurrentUser",
    "get_current_user",
    "get_genai_client",
    "get_llm_gateway",
    "get_redis",
    "get_settings_dep",
    "require_non_prod",
    "require_workspace_role",
]

_bearer_scheme = HTTPBearer(auto_error=False)


def get_settings_dep() -> Settings:
    return get_settings()


def require_non_prod(settings: Settings = Depends(get_settings_dep)) -> None:
    """Dependency for routes that must not exist in prod (e.g. the debug router).
    404s rather than 403s so the route's existence isn't confirmed to a caller."""
    if settings.env == "prod":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


@lru_cache
def _redis_pool(redis_url: str) -> Redis:
    return cast(Redis, Redis.from_url(redis_url, decode_responses=False))


def get_redis(settings: Settings = Depends(get_settings_dep)) -> Redis:
    return _redis_pool(settings.redis_url)


@lru_cache
def _genai_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)


def get_genai_client(settings: Settings = Depends(get_settings_dep)) -> genai.Client:
    return _genai_client(settings.gemini_api_key)


def get_llm_gateway(
    session: AsyncSession = Depends(get_session),
    client: genai.Client = Depends(get_genai_client),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings_dep),
) -> LLMGateway:
    limiter = RedisRateLimiter(redis, rpm_limit=settings.gemini_rpm_limit)
    return LLMGateway(
        client,
        limiter=limiter,
        llm_calls=LLMCallRepository(session),
        pricing=ModelPricingRepository(session),
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        claims = decode_access_token(credentials.credentials, settings=settings)
    except InvalidAccessToken as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid or expired access token"
        ) from exc

    user = await UserRepository(session).get(claims.sub)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired access token")
    return user


@dataclass(frozen=True)
class CurrentUser:
    user: User
    membership: WorkspaceMember


def require_workspace_role(minimum: str):  # type: ignore[no-untyped-def]
    """Dependency factory: the caller must be a member of `{workspace_id}` (from the
    path) with role >= `minimum`. A non-member gets 404, not 403 — confirming that a
    workspace id exists to someone outside it is itself tenant leakage
    (docs/system-design.md section 18.1, "Cross-tenant data access")."""

    async def _dep(
        workspace_id: uuid.UUID = Path(...),
        user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> CurrentUser:
        membership = await WorkspaceMemberRepository(session).get(workspace_id, user.id)
        if membership is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
        if not has_at_least(membership.role, minimum):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role for this action")
        return CurrentUser(user=user, membership=membership)

    return _dep
