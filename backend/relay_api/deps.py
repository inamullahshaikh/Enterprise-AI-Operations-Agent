"""Shared FastAPI dependencies: settings/session wiring, auth, workspace
membership/RBAC (docs/system-design.md section 28, "Phase 1 - Foundation"), and
the LLM gateway / agent-run dispatch wiring added in "Phase 2 - Agent core".
"""

import uuid
from collections.abc import Awaitable, Callable
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
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository
from relay_core.db.session import get_session
from relay_core.llm.gateway import LLMGateway, build_llm_gateway
from relay_core.security.crypto import LocalKMS, build_kms
from relay_core.security.jwt import InvalidAccessToken, decode_access_token
from relay_core.security.rbac import has_at_least
from relay_core.storage.object_store import ObjectStore, build_object_store

__all__ = [
    "CurrentUser",
    "IngestDispatcher",
    "RunDispatcher",
    "get_current_user",
    "get_genai_client",
    "get_ingest_dispatcher",
    "get_kms",
    "get_llm_gateway",
    "get_object_store",
    "get_redis",
    "get_run_dispatcher",
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
    return build_llm_gateway(session, redis, settings, client=client)


def get_object_store(settings: Settings = Depends(get_settings_dep)) -> ObjectStore:
    return build_object_store(settings)


def get_kms(settings: Settings = Depends(get_settings_dep)) -> LocalKMS:
    return build_kms(settings)


RunDispatcher = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]


def get_run_dispatcher() -> RunDispatcher:
    """Enqueues a queued `agent_runs` row for the worker to pick up. Kept as its
    own dependency (rather than calling `run_agent.delay(...)` straight from the
    router) so integration tests can override it to run the agent graph inline
    against the test database/Redis instead of needing a real Celery worker.

    Takes `workspace_id` explicitly (not just `run_id`) because every repository
    lookup in this codebase is tenant-scoped by construction (`WorkspaceScopedRepository`,
    docs/system-design.md section 14.4) — the worker has no way to look up a run
    by id alone, so the id it needs is handed to it here instead.
    """

    async def _dispatch(workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
        from relay_worker.tasks.agent import run_agent

        run_agent.delay(str(workspace_id), str(run_id))

    return _dispatch


IngestDispatcher = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]


def get_ingest_dispatcher() -> IngestDispatcher:
    """Enqueues a `documents` row for `relay_worker.tasks.ingest` to process — the same
    override-for-tests shape as `get_run_dispatcher` above, for the same reason."""

    async def _dispatch(workspace_id: uuid.UUID, document_id: uuid.UUID) -> None:
        from relay_worker.tasks.ingest import run_ingest_document

        run_ingest_document.delay(str(workspace_id), str(document_id))

    return _dispatch


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
