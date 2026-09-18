"""Phase 8 B5: the stalled-run watchdog (section 19.1) and the retention sweep (section 14.5)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.attachments import Attachment
from relay_core.db.models.audit import AuditLog
from relay_core.db.models.conversations import Conversation
from relay_core.db.models.llm import LLMCall
from relay_core.db.models.policies import WorkspacePolicy
from relay_core.db.models.runs import AgentRun
from relay_core.db.models.tool_calls import ToolCall
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.events.publisher import EventPublisher
from relay_core.maintenance import STALLED_ERROR_CODE, apply_retention, fail_stalled_runs
from tests.integration.scripted_model import register_workspace_and_conversation

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


async def _workspace(client, db_session, email):
    _, ws, conv = await register_workspace_and_conversation(client, email)
    user = await UserRepository(db_session).get_by_email(email)
    assert user is not None
    return ws, conv, user.id


async def _run(db_session, ws, conv, user_id, status: str, started: datetime) -> AgentRun:
    run = await AgentRunRepository(db_session).create(
        workspace_id=ws, conversation_id=conv, user_id=user_id, trigger_message_id=None
    )
    run.status, run.started_at = status, started
    await db_session.flush()
    return run


def _tool_call(ws, run_id, *, at: datetime, output=None) -> ToolCall:
    return ToolCall(
        workspace_id=ws,
        run_id=run_id,
        plan_step_id="s1",
        llm_name="db__run_sql",
        arguments={},
        risk="read",
        status="succeeded",
        output=output,
        started_at=at,
        created_at=at,
    )


async def test_watchdog_fails_only_runs_with_no_recent_activity(
    client: AsyncClient, db_session: AsyncSession, redis_client: Redis
) -> None:
    ws, conv, user_id = await _workspace(client, db_session, "watchdog@example.com")
    long_ago = NOW - timedelta(minutes=30)
    stalled = await _run(db_session, ws, conv, user_id, "running", long_ago)
    busy = await _run(db_session, ws, conv, user_id, "running", long_ago)
    parked = await _run(db_session, ws, conv, user_id, "awaiting_approval", long_ago)
    db_session.add_all(
        [
            _tool_call(ws, stalled.id, at=NOW - timedelta(minutes=20)),
            _tool_call(ws, busy.id, at=NOW - timedelta(minutes=2)),
        ]
    )
    await db_session.flush()

    assert await fail_stalled_runs(db_session, EventPublisher(redis_client), now=NOW) == 1
    await db_session.flush()
    for run in (stalled, busy, parked):
        await db_session.refresh(run)
    assert (stalled.status, stalled.error_code) == ("failed", STALLED_ERROR_CODE)
    assert busy.status == "running"
    assert parked.status == "awaiting_approval"


class _Blobs:
    def __init__(self, fail: bool = False) -> None:
        self.deleted: list[str] = []
        self.fail = fail

    async def delete(self, key: str) -> None:
        if self.fail:
            raise RuntimeError("store down")
        self.deleted.append(key)


class _Threads:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


async def test_retention_clears_old_data_and_keeps_the_audit_trail(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    ws, conv, user_id = await _workspace(client, db_session, "retention@example.com")
    # Leave the other workspaces the suite created out of this sweep.
    await db_session.execute(
        update(WorkspacePolicy)
        .where(WorkspacePolicy.workspace_id != ws)
        .values(data_retention_days=0)
    )
    old, recent = NOW - timedelta(days=120), NOW - timedelta(days=1)
    run = await _run(db_session, ws, conv, user_id, "completed", old)
    db_session.add_all(
        [
            _tool_call(ws, run.id, at=old, output={"rows": [1]}),
            _tool_call(ws, run.id, at=recent, output={"rows": [2]}),
            LLMCall(
                workspace_id=ws,
                run_id=run.id,
                node="plan",
                model="m",
                cost_usd=Decimal("0.1"),
                latency_ms=1,
                status="ok",
                created_at=old,
            ),
            Attachment(
                workspace_id=ws,
                conversation_id=conv,
                filename="a.csv",
                mime_type="text/csv",
                size_bytes=1,
                blob_key="attachments/old.csv",
                kind="table",
                uploaded_by=user_id,
                created_at=old,
            ),
        ]
    )
    await db_session.execute(
        update(Conversation).where(Conversation.id == conv).values(updated_at=old)
    )
    await db_session.flush()

    blobs, threads = _Blobs(), _Threads()
    results = await apply_retention(db_session, blobs, threads, now=NOW)

    counts = results[ws]
    assert not isinstance(counts, str), counts
    assert (counts.tool_outputs_cleared, counts.attachments_deleted) == (1, 1)
    assert blobs.deleted == ["attachments/old.csv"]
    assert threads.deleted == [str(conv)]

    outputs = (
        await db_session.execute(select(ToolCall.output).where(ToolCall.run_id == run.id))
    ).scalars()
    assert sorted(outputs, key=str) == sorted([None, {"rows": [2]}], key=str)
    llm = await db_session.execute(select(LLMCall).where(LLMCall.run_id == run.id))
    assert len(llm.all()) == 1
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.workspace_id == ws, AuditLog.action == "retention.applied"
            )
        )
    ).scalar_one()
    assert audit.details["tool_outputs_cleared"] == 1


async def test_zero_retention_is_skipped_and_one_failure_does_not_stop_the_sweep(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    ws_skip, conv_skip, user_a = await _workspace(client, db_session, "ret-skip@example.com")
    ws_fail, conv_fail, user_b = await _workspace(client, db_session, "ret-fail@example.com")
    ws_ok, _, _ = await _workspace(client, db_session, "ret-ok@example.com")
    await db_session.execute(
        update(WorkspacePolicy)
        .where(WorkspacePolicy.workspace_id.not_in([ws_fail, ws_ok]))
        .values(data_retention_days=0)
    )
    old = NOW - timedelta(days=365)
    run = await _run(db_session, ws_skip, conv_skip, user_a, "completed", old)
    db_session.add(_tool_call(ws_skip, run.id, at=old, output={"keep": True}))
    db_session.add(
        Attachment(
            workspace_id=ws_fail,
            conversation_id=conv_fail,
            filename="b.csv",
            mime_type="text/csv",
            size_bytes=1,
            blob_key="attachments/b.csv",
            kind="table",
            uploaded_by=user_b,
            created_at=old,
        )
    )
    await db_session.flush()

    results = await apply_retention(db_session, _Blobs(fail=True), _Threads(), now=NOW)

    assert ws_skip not in results
    assert isinstance(results[ws_fail], str)
    assert not isinstance(results[ws_ok], str)
    kept = await db_session.execute(select(ToolCall.output).where(ToolCall.run_id == run.id))
    assert kept.scalar_one() == {"keep": True}
