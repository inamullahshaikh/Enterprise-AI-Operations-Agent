"""Per-workspace run permits (docs/system-design.md section 19.3).

A sorted set per workspace, one member per run holding a permit, scored by when that permit
lapses. Expired members are dropped before counting, so a worker killed mid-run leaks its permit
only until the TTL passes, never forever. The worker takes a permit before it runs or resumes a
graph and returns it in a `finally`; a run parked at `awaiting_approval` has exited the worker by
then, so it holds no permit while it waits for a human.
"""

import time
import uuid

from redis.asyncio import Redis

# Well past any run's `max_wall_seconds`, so a live run's permit never lapses under it.
PERMIT_TTL_S = 30 * 60


def _key(workspace_id: uuid.UUID) -> str:
    return f"relay:run_permits:{workspace_id}"


async def acquire_permit(
    redis: Redis, workspace_id: uuid.UUID, run_id: uuid.UUID, limit: int
) -> bool:
    # ponytail: check-then-add is not atomic, so two workers racing for the last permit can both
    # get it; a Lua script closes that if a fourth concurrent run ever matters.
    key, now = _key(workspace_id), time.time()
    await redis.zremrangebyscore(key, 0, now)
    await redis.zadd(key, {str(run_id): now + PERMIT_TTL_S})
    await redis.expire(key, PERMIT_TTL_S)
    if await redis.zcard(key) <= limit:
        return True
    await redis.zrem(key, str(run_id))
    return False


async def release_permit(redis: Redis, workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
    await redis.zrem(_key(workspace_id), str(run_id))
