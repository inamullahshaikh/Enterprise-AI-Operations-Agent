"""Redis Streams event publisher (docs/system-design.md section 16.2): each run
gets its own stream, `XADD`-ed by the worker and `XREAD`-ed by the SSE endpoint
so a client can resume with `Last-Event-ID` after a reconnect without losing
events raised while it was disconnected.
"""

import json
import uuid
from typing import Any

from redis.asyncio import Redis

_STREAM_MAXLEN = 2000
_STREAM_TTL_SECONDS = 3600


class EventPublisher:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def publish(self, run_id: uuid.UUID, event_type: str, payload: dict[str, Any]) -> None:
        key = stream_key(run_id)
        await self.redis.xadd(
            key,
            {"type": event_type, "data": json.dumps(payload, default=str)},
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
        # Refreshed on every publish rather than set once, so a long-running run
        # never has its backlog expire out from under a client mid-stream.
        await self.redis.expire(key, _STREAM_TTL_SECONDS)


def stream_key(run_id: uuid.UUID) -> str:
    """Shared with the SSE endpoint (`relay_api/routers/runs.py`), which reads
    from the same key this publishes to."""
    return f"run:{run_id}"
