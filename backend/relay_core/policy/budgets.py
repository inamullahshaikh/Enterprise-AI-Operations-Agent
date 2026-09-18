"""The monthly workspace budget (docs/system-design.md section 19.2), checked when a message is
enqueued. The per-run budget lives with the loop that spends it
(`relay_core.agent.nodes.execute_step.enforce_budget`).

This month's spend is cached in Redis for a minute because the check runs on every message.
`finalize` drops the cache when a run finishes, so a workspace that crosses its cap is refused
on its next message, not a minute later. Redis being down means an uncached read, never a
refused message.
"""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.repositories.llm_calls import LLMCallRepository, month_window

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 60


def _cache_key(workspace_id: uuid.UUID) -> str:
    return f"budget:{workspace_id}:{datetime.now(UTC):%Y-%m}"


async def month_spend(session: AsyncSession, redis: Redis, workspace_id: uuid.UUID) -> Decimal:
    key = _cache_key(workspace_id)
    try:
        cached = await redis.get(key)
        if cached is not None:
            return Decimal(cached.decode() if isinstance(cached, bytes) else cached)
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.warning("budget cache unavailable", exc_info=True)

    rows = await LLMCallRepository(session).usage_breakdown(workspace_id, group_by="model")
    spend = sum((r.cost_usd for r in rows), Decimal(0))
    try:
        await redis.set(key, str(spend), ex=_CACHE_TTL_S)
    except Exception:  # noqa: BLE001
        logger.warning("budget cache unavailable", exc_info=True)
    return spend


async def invalidate_month_spend(redis: Redis, workspace_id: uuid.UUID) -> None:
    try:
        await redis.delete(_cache_key(workspace_id))
    except Exception:  # noqa: BLE001
        logger.warning("budget cache unavailable", exc_info=True)


def budget_resets_at() -> datetime:
    return month_window()[1]
