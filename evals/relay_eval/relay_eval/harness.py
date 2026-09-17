"""The eval harness core (docs/system-design.md section 21.5): runs each case through the
real graph (`relay_core.agent.runner.run_agent_once`) and repositories, the same way
`backend/tests/integration/test_chat_flow.py` does. Uses a real Gemini client
(`settings.gemini_api_key`), a real Postgres/Redis, and an in-memory LangGraph checkpointer
shared by a case's first run and every resume of it — the harness is one process, so the
resume reads back exactly the checkpoint the run parked on.

**Write cases park and resume** (Phase 5). A run that stops at `approval_gate` is answered the
way a human would answer it: the harness calls the real decision route function
(`relay_api.routers.approvals.decide_approval`, so its RBAC/expiry/partial-batch logic is what
runs) according to the case's `approval_decision`, then resumes the run, and repeats until the
run stops asking. The `full` profile's gmail/google_calendar/web_search installations point at
the mock service (section 21.2) and its `mcp` installation at the sample ticketing server. Both
are reset before each such case, so their write counts belong to that case alone.

A case message may contain `{mock_services_url}`, for a page URL that differs between Docker and
CI.
"""

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from langgraph.checkpoint.memory import MemorySaver
from redis.asyncio import Redis
from relay_api.deps import CurrentUser
from relay_api.routers.approvals import DecisionRequest, decide_approval
from relay_core.agent.runner import resume_agent_once, run_agent_once
from relay_core.config import Settings, get_settings
from relay_core.db.models.approvals import Approval
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.approvals import ApprovalRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.db.repositories.tool_calls import ToolCallRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceMemberRepository
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
# A run that is still asking after this many decisions is looping, not working.
_MAX_APPROVAL_ROUNDS = 10


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

    @property
    def violations(self) -> int:
        return sum(r.violations for r in self.results)


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
                await run_case(
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


async def run_case(
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
            session, settings, kms, redis, user_id, case.connector_profile
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
            content=case.message.replace("{mock_services_url}", settings.mock_services_url),
        )
        run = await AgentRunRepository(session).create(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            user_id=user_id,
            trigger_message_id=message.id,
        )
        await session.commit()

    uses_mocks = case.connector_profile == "full"
    baseline = 0
    if uses_mocks:
        for base_url in (settings.mock_services_url, _ticketing_base(settings)):
            async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
                (await http.post("/_reset")).raise_for_status()
        baseline = await _mock_write_count(settings)

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
    gated_ids, approved_ids = await _decide_until_done(
        case,
        workspace_id,
        run.id,
        user_id,
        sessionmaker=sessionmaker,
        redis=redis,
        settings=settings,
        checkpointer=checkpointer,
    )
    latency_s = time.monotonic() - started

    side_effects = (
        await _mock_write_count(settings) - baseline if uses_mocks else None
    )

    async with sessionmaker() as session:
        agent_run = await AgentRunRepository(session).get(workspace_id, run.id)
        assert agent_run is not None, f"agent_runs row for case {case.key!r} disappeared"
        tool_calls = await ToolCallRepository(session).list_for_run(workspace_id, run.id)
        final_answer = None
        if agent_run.final_message_id is not None:
            final_message = await MessageRepository(session).get(
                workspace_id, agent_run.final_message_id
            )
            final_answer = final_message.content if final_message is not None else None
        return await score_case(
            case,
            agent_run,
            tool_calls,
            settings,
            latency_s,
            final_answer=final_answer,
            gated_ids=gated_ids,
            approved_ids=approved_ids,
            side_effects=side_effects,
        )


async def _decide_until_done(
    case: EvalCase,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    checkpointer: MemorySaver,
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """Plays the approver. Returns every `tool_calls` id put up for approval and the subset the
    case's script approved — the latter is the ground truth `approval_violations` scores
    against."""
    gated: set[uuid.UUID] = set()
    approved: set[uuid.UUID] = set()
    decisions: list[dict[str, Any]] = []

    async def _capture(_ws: uuid.UUID, _run: uuid.UUID, decision: dict[str, Any]) -> None:
        decisions.append(decision)

    for _ in range(_MAX_APPROVAL_ROUNDS):
        async with sessionmaker() as session:
            run = await AgentRunRepository(session).get(workspace_id, run_id)
            if run is None or run.status != "awaiting_approval":
                break
            pending = [
                a
                for a in await ApprovalRepository(session).list_pending(workspace_id)
                if a.run_id == run_id
            ]
            if not pending:
                break
            approval = pending[0]
            user = await UserRepository(session).get(user_id)
            membership = await WorkspaceMemberRepository(session).get(workspace_id, user_id)
            assert user is not None and membership is not None
            body, script_approved = _scripted_decision(
                approval, case.expectations.approval_decision
            )
            # The route commits, then hands the resume to `dispatch` — captured here so it runs
            # after this session closes rather than on top of it.
            await decide_approval(
                body,
                workspace_id=workspace_id,
                approval_id=approval.id,
                current=CurrentUser(user=user, membership=membership),
                session=session,
                dispatch=_capture,
            )
        gated.update(approval.tool_call_ids)
        approved.update(script_approved)

        async with sessionmaker() as session:
            await resume_agent_once(
                workspace_id,
                run_id,
                decisions.pop(),
                session=session,
                redis=redis,
                settings=settings,
                checkpointer=checkpointer,
            )
            await session.commit()
    return gated, approved


def _scripted_decision(
    approval: Approval, script: str
) -> tuple[DecisionRequest, list[uuid.UUID]]:
    ids = list(approval.tool_call_ids)
    if script == "reject_all":
        return DecisionRequest(action="reject", reason="eval harness: reject_all"), []
    if script == "approve_first":
        return DecisionRequest(action="approve", item_ids=ids[:1]), ids[:1]
    return DecisionRequest(action="approve"), ids


async def _mock_write_count(settings: Settings) -> int:
    """Drafts + sent messages + calendar events held by the mock service, plus tickets created
    and comments added on the ticketing server."""
    async with httpx.AsyncClient(base_url=settings.mock_services_url, timeout=10) as http:
        mock_stats = await http.get("/_stats")
    async with httpx.AsyncClient(base_url=_ticketing_base(settings), timeout=10) as http:
        stats = await http.get("/_stats")
    for resp in (mock_stats, stats):
        resp.raise_for_status()
    counts = mock_stats.json()
    # Every calendar, not just `primary`: the mock counts events rather than listing one id.
    written = sum(int(counts[key]) for key in ("drafts", "sent", "events"))
    return written + sum(stats.json().values())


def _ticketing_base(settings: Settings) -> str:
    return settings.mcp_ticketing_url.removesuffix("/mcp")
