"""Run detail & SSE routes (docs/system-design.md sections 15.4, 16).

Runs are only visible to the user whose conversation triggered them, same
ownership rule as `relay_api/routers/conversations.py` — cross-workspace-member
visibility into someone else's runs is an audit-log (Phase 8) concern, not this
endpoint's.
"""

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import CurrentUser, get_redis, require_workspace_role
from relay_core.db.models.runs import AgentRun
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.session import get_session
from relay_core.events.publisher import stream_key
from relay_core.events.types import TERMINAL_EVENTS
from relay_core.security.rbac import Role

router = APIRouter(prefix="/workspaces/{workspace_id}/runs", tags=["runs"])

_BLOCK_MS = 15_000
_READ_COUNT = 100


class RunOut(BaseModel):
    id: uuid.UUID
    status: str
    route: str | None
    plan: dict[str, Any] | None
    missing_capabilities: dict[str, Any] | None
    capability_snapshot: list[str] | None
    final_message_id: uuid.UUID | None
    error_code: str | None
    error_message: str | None
    llm_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    thought_tokens: int
    cost_usd: Decimal

    @classmethod
    def from_model(cls, r: AgentRun) -> "RunOut":
        return cls(
            id=r.id,
            status=r.status,
            route=r.route,
            plan=r.plan,
            missing_capabilities=r.missing_capabilities,
            capability_snapshot=r.capability_snapshot,
            final_message_id=r.final_message_id,
            error_code=r.error_code,
            error_message=r.error_message,
            llm_calls=r.llm_calls,
            tool_calls=r.tool_calls,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            thought_tokens=r.thought_tokens,
            cost_usd=r.cost_usd,
        )


async def _owned_run(
    session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID, user_id: uuid.UUID
) -> AgentRun:
    run = await AgentRunRepository(session).get(workspace_id, run_id)
    if run is None or run.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return run


@router.get("/{run_id}", response_model=RunOut)
async def get_run(
    workspace_id: uuid.UUID = Path(...),
    run_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    run = await _owned_run(session, workspace_id, run_id, current.user.id)
    return RunOut.from_model(run)


@router.get("/{run_id}/events")
async def run_events(
    request: Request,
    workspace_id: uuid.UUID = Path(...),
    run_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    await _owned_run(session, workspace_id, run_id, current.user.id)
    last_id = request.headers.get("last-event-id", "0")
    return StreamingResponse(
        _event_stream(redis, run_id, last_id, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _event_stream(
    redis: Redis, run_id: uuid.UUID, last_id: str, request: Request
) -> AsyncIterator[bytes]:
    key = stream_key(run_id)
    current_id = last_id
    while True:
        if await request.is_disconnected():
            return
        response = await redis.xread({key: current_id}, count=_READ_COUNT, block=_BLOCK_MS)
        if not response:
            yield b": heartbeat\n\n"
            continue
        for _stream_name, entries in response:
            for entry_id, fields in entries:
                current_id = entry_id
                event_type = fields[b"type"].decode()
                data = fields[b"data"].decode()
                eid = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
                yield f"id: {eid}\nevent: {event_type}\ndata: {data}\n\n".encode()
                if event_type in TERMINAL_EVENTS:
                    return
