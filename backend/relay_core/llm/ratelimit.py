"""Fixed-window rate limiting over Redis. `hit` is the keyed counter; the per-model LLM
limiter (docs/system-design.md section 9.1, `GEMINI_RPM_LIMIT`) and the HTTP limits
(section 15.6, `relay_api.ratelimit`) are both callers of it.

Uses a fixed-window counter rather than a true token bucket: it's a few lines
instead of a Lua script, and for a per-minute request cap the burst behavior at
window edges doesn't matter here — good enough for a single-process dev/portfolio
deployment where the alternative is no rate limiting at all.
"""

import time

from redis.asyncio import Redis


class RateLimitExceeded(Exception):
    def __init__(self, model: str, retry_after_s: float) -> None:
        self.model = model
        self.retry_after_s = retry_after_s
        super().__init__(f"Rate limit exceeded for {model}, retry after {retry_after_s:.1f}s")


async def hit(redis: Redis, key: str, limit: int, window_s: int = 60) -> tuple[int, float]:
    """Counts one request against `key`'s current window. Returns (requests left, seconds until
    the window resets); left is negative once the limit is exceeded."""
    now = time.time()
    window_key = f"{key}:{int(now // window_s)}"
    count = await redis.incr(window_key)
    if count == 1:
        await redis.expire(window_key, window_s)
    return limit - count, window_s - (now % window_s)


class RedisRateLimiter:
    def __init__(self, redis: Redis, *, rpm_limit: int) -> None:
        self.redis = redis
        self.rpm_limit = rpm_limit

    async def acquire(self, model: str) -> None:
        left, retry_after = await hit(self.redis, f"relay:llm_rpm:{model}", self.rpm_limit)
        if left < 0:
            raise RateLimitExceeded(model, retry_after)
