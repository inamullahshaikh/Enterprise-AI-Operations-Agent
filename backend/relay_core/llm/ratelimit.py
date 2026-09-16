"""Per-model rate limiting (docs/system-design.md section 9.1: "Redis token bucket
per model"), backed by `GEMINI_RPM_LIMIT` requests/minute.

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


class RedisRateLimiter:
    def __init__(self, redis: Redis, *, rpm_limit: int) -> None:
        self.redis = redis
        self.rpm_limit = rpm_limit

    async def acquire(self, model: str) -> None:
        window = int(time.time() // 60)
        key = f"relay:llm_rpm:{model}:{window}"
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, 60)
        if count > self.rpm_limit:
            retry_after = 60 - (time.time() % 60)
            raise RateLimitExceeded(model, retry_after)
