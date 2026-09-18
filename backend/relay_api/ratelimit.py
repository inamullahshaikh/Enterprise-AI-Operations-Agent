"""HTTP rate limits (docs/system-design.md section 15.6) as FastAPI dependencies.

Fixed window, via the same `relay_core.llm.ratelimit.hit` counter the LLM limiter uses. Every
guarded response carries `RateLimit-Limit`/`RateLimit-Remaining` for the tightest window it
counted against; an exceeded one returns 429 with `Retry-After`. A Redis that cannot be read
lets the request through, the same posture as the connector circuit breaker: a limiter outage
must not become an API outage.
"""

import logging
import math
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Path, Response, status
from redis.asyncio import Redis

from relay_api.deps import get_current_user, get_redis, get_settings_dep
from relay_core.config import Settings
from relay_core.db.models.identity import User
from relay_core.llm.ratelimit import hit

logger = logging.getLogger(__name__)


async def _enforce(response: Response, redis: Redis, limits: list[tuple[str, int]]) -> None:
    try:
        results = [(limit, *await hit(redis, key, limit)) for key, limit in limits]
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.warning("rate limiter unavailable; allowing request", exc_info=True)
        return
    limit, left, reset = min(results, key=lambda r: r[1])
    headers = {"RateLimit-Limit": str(limit), "RateLimit-Remaining": str(max(left, 0))}
    if left < 0:
        headers["Retry-After"] = str(math.ceil(reset))
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Rate limit exceeded", headers)
    response.headers.update(headers)


def message_rate_limit() -> Callable[..., Awaitable[None]]:
    async def _dep(
        response: Response,
        workspace_id: uuid.UUID = Path(...),
        user: User = Depends(get_current_user),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> None:
        await _enforce(
            response,
            redis,
            [
                (f"relay:rl:msg:user:{user.id}", settings.rate_limit_messages_per_user_min),
                (f"relay:rl:msg:ws:{workspace_id}", settings.rate_limit_messages_per_workspace_min),
            ],
        )

    return _dep


def connector_test_rate_limit() -> Callable[..., Awaitable[None]]:
    async def _dep(
        response: Response,
        workspace_id: uuid.UUID = Path(...),
        redis: Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings_dep),
    ) -> None:
        await _enforce(
            response,
            redis,
            [(f"relay:rl:conntest:ws:{workspace_id}", settings.rate_limit_connector_tests_min)],
        )

    return _dep
