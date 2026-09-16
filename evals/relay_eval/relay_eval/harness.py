"""The eval harness core (docs/system-design.md section 21.5): runs each case through the
real graph (`relay_core.agent.runner.run_agent_once`) and repositories, the same way
`backend/tests/integration/test_chat_flow.py` does — no mocked connectors yet (section 21.2's
`full` profile and its fixture-recorded mocks wait for Gmail/Calendar/HubSpot to exist to mock,
Phase 5+). Uses a real Gemini client (`settings.gemini_api_key`), a real Postgres/Redis, and an
in-memory LangGraph checkpointer — Phase 3 has no `interrupt()` to resume from, so there's
nothing checkpoint-durability-specific for this harness to exercise (the same reasoning
`test_chat_flow.py`'s own docstring gives for using `MemorySaver` there).
"""

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from redis.asyncio import Redis
from relay_core.agent.runner import run_agent_once
from relay_core.config import Settings, get_settings
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.security.crypto import LocalKMS, build_kms
from relay_core.storage.object_store import ObjectStore, build_object_store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from relay_eval.cases import EvalCase, load_suite
from relay_eval.scoring import CaseResult, score_case
from relay_eval.workspace_setup import (
    attach_csv_fixture,
    ensure_eval_user,
    ensure_workspace_for_profile,
    new_conversation,
)

_EVALS_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class SuiteReport:
    suite: str
    results: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / len(self.results) if self.results else 0.0


async def run_suite(suite: str, settings: Settings | None = None) -> SuiteReport:
    settings = settings or get_settings()
    cases = load_suite(_EVALS_ROOT / "suites", suite)
    report = SuiteReport(suite=suite)
    if not cases:
        return report

    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url)
    object_store = build_object_store(settings)
    kms = build_kms(settings)
    checkpointer = MemorySaver()

    try:
        async with sessionmaker() as session:
            user_id = await ensure_eval_user(session)
            await session.commit()

        for case in cases:
            report.results.append(
                await _run_case(
                    case,
                    sessionmaker=sessionmaker,
                    redis=redis,
                    object_store=object_store,
                    kms=kms,
                    settings=settings,
                    user_id=user_id,
                    checkpointer=checkpointer,
                )
            )
    finally:
        await redis.aclose()
        await engine.dispose()

    return report


async def _run_case(
    case: EvalCase,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    object_store: ObjectStore,
    kms: LocalKMS,
    settings: Settings,
    user_id: uuid.UUID,
    checkpointer: MemorySaver,
) -> CaseResult:
    async with sessionmaker() as session:
        workspace_id = await ensure_workspace_for_profile(
            session, settings, kms, user_id, case.connector_profile
        )
        conversation_id = await new_conversation(session, workspace_id, user_id)
        if case.connector_profile == "csv_only" and case.csv_fixture:
            await attach_csv_fixture(
                session,
                object_store,
                workspace_id,
                conversation_id,
                user_id,
                _EVALS_ROOT / "fixtures",
                case.csv_fixture,
            )
        message = await MessageRepository(session).create(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            role="user",
            content=case.message,
        )
        run = await AgentRunRepository(session).create(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            user_id=user_id,
            trigger_message_id=message.id,
        )
        await session.commit()

    started = time.monotonic()
    async with sessionmaker() as session:
        await run_agent_once(
            workspace_id,
            run.id,
            session=session,
            redis=redis,
            settings=settings,
            checkpointer=checkpointer,
        )
        await session.commit()
    latency_s = time.monotonic() - started

    async with sessionmaker() as session:
        agent_run = await AgentRunRepository(session).get(workspace_id, run.id)
        assert agent_run is not None, f"agent_runs row for case {case.key!r} disappeared"
        tool_calls = await ToolCallRepository(session).list_for_run(workspace_id, run.id)
        return await score_case(case, agent_run, tool_calls, settings, latency_s)
