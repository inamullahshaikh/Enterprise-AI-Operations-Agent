"""Per-installation circuit breaker (docs/system-design.md section 19.1).

A connector that has started failing keeps failing for a while — an expired token, a server
that is down, a network path that is broken. Retrying it on every step of every run spends the
run's budget and its wall clock on a call that cannot work, and, once a workspace has two
installations providing the same capability, it does that instead of using the one that works.
`FAILURE_THRESHOLD` failures inside `WINDOW_S` takes the installation out of binding for
`OPEN_S`; the resolver then reports its capabilities as provided by the next installation in
priority order, and no planner change is needed (ADR-0013 decision 7).

Only *connector* failures count. A tool that returns a clean business error — "no such account",
"the date is in the past" — worked exactly as intended, and a breaker that counted those would
open on a healthy connector being asked the wrong questions. `ToolExecutor` records a failure
only from the branch where the call raised or timed out.

**Redis being unavailable must not break tool calls.** Every method below swallows its own
errors and answers as though the breaker were closed. The breaker is an optimization over a
failing dependency; letting it become a second failing dependency would be worse than not
having it.
"""

import logging
import uuid

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 5
WINDOW_S = 60
OPEN_S = 120


class CircuitBreaker:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def is_open(self, installation_id: uuid.UUID) -> bool:
        try:
            return bool(await self.redis.exists(_open_key(installation_id)))
        except Exception:  # noqa: BLE001 - a breaker that can't be read is closed
            logger.warning("circuit breaker unreadable; treating as closed", exc_info=True)
            return False

    async def open_among(self, installation_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        """Which of `installation_ids` are currently open, in one round trip. Binding asks this
        about every candidate at once rather than one `is_open` per row."""
        ids = sorted(installation_ids)
        if not ids:
            return set()
        try:
            flags = await self.redis.mget([_open_key(i) for i in ids])
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.warning("circuit breaker unreadable; treating as closed", exc_info=True)
            return set()
        return {i for i, flag in zip(ids, flags, strict=True) if flag is not None}

    async def record_failure(self, installation_id: uuid.UUID) -> bool:
        """Counts one connector failure. Returns True only on the call that opened the breaker,
        so the caller marks the installation degraded once rather than on every later failure.

        The counter is a fixed window, like `relay_core.llm.ratelimit`: five failures spread
        either side of a minute boundary won't trip it, and that is fine — a connector failing
        that steadily will trip on the next window.
        """
        try:
            count = await self.redis.incr(_fail_key(installation_id))
            if count == 1:
                await self.redis.expire(_fail_key(installation_id), WINDOW_S)
            if count < FAILURE_THRESHOLD:
                return False
            # `nx=True` is what makes "just opened" true exactly once: a sixth and seventh
            # failure inside the same window find the key already set.
            return bool(await self.redis.set(_open_key(installation_id), "1", ex=OPEN_S, nx=True))
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.warning("circuit breaker unwritable; failure not counted", exc_info=True)
            return False

    async def record_success(self, installation_id: uuid.UUID) -> bool:
        """Clears the failure count. Returns True only when there was something to clear, so a
        healthy installation's every call doesn't turn into a database write clearing a health
        message that was never set."""
        try:
            cleared = await self.redis.delete(
                _fail_key(installation_id), _open_key(installation_id)
            )
            return bool(cleared)
        except Exception:  # noqa: BLE001 - see the module docstring
            return False


def _fail_key(installation_id: uuid.UUID) -> str:
    return f"relay:breaker:fail:{installation_id}"


def _open_key(installation_id: uuid.UUID) -> str:
    return f"relay:breaker:open:{installation_id}"
