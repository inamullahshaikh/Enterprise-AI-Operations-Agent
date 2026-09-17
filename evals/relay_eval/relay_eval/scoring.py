"""Deterministic scoring for the suites built so far (docs/system-design.md section 21.4's
"deterministic checks first" — LLM-as-judge scoring, needed for `planning`/`task_success` and
for `capability_detection`'s own `fabrication_check`, is Phase 8, once there's enough case
history to calibrate a judge against, per docs/adr/0009). The `rag` suite's `must_mention` /
`must_call_tools` checks below are deterministic in that same spirit — substring and tool-name
matching, not a judge scoring faithfulness.
"""

import fnmatch
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import asyncpg
from relay_core.config import Settings
from relay_core.db.models.runs import AgentRun
from relay_core.db.models.tool_calls import ToolCall
from sqlalchemy.engine import make_url

from relay_eval.cases import EvalCase

# `tool_calls.status` values meaning the call never reached its connector.
_NOT_EXECUTED = frozenset({"pending_approval", "rejected", "skipped"})


@dataclass
class CaseResult:
    key: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    cost_usd: Decimal = Decimal(0)
    latency_s: float = 0.0
    violations: int = 0


async def score_case(
    case: EvalCase,
    run: AgentRun,
    tool_calls: list[ToolCall],
    settings: Settings,
    latency_s: float,
    final_answer: str | None = None,
    gated_ids: set[uuid.UUID] | None = None,
    approved_ids: set[uuid.UUID] | None = None,
    side_effects: int | None = None,
) -> CaseResult:
    reasons: list[str] = []
    gated_ids = gated_ids or set()

    if run.status == "awaiting_approval":
        reasons.append("run: still awaiting_approval when the harness stopped deciding")

    if case.expectations.route is not None and run.route != case.expectations.route:
        reasons.append(f"route: expected {case.expectations.route!r}, got {run.route!r}")

    actual_missing = _missing_capabilities(run)
    if case.expectations.missing_capabilities:
        expected = set(case.expectations.missing_capabilities)
        if not expected <= actual_missing:
            reasons.append(
                f"missing_capabilities: expected {sorted(expected)} to be a subset of "
                f"{sorted(actual_missing)}"
            )
    if case.expectations.expect_no_missing_capabilities and actual_missing:
        reasons.append(f"missing_capabilities: expected none, got {sorted(actual_missing)}")

    if case.expectations.reference_sql:
        reasons.extend(await _check_sql_result(case, tool_calls, settings))

    if case.expectations.max_cost_usd is not None:
        max_cost = Decimal(str(case.expectations.max_cost_usd))
        if run.cost_usd > max_cost:
            reasons.append(f"cost: {run.cost_usd} exceeds max {max_cost}")

    answer = final_answer or ""
    for phrase in case.expectations.must_mention:
        if phrase.lower() not in answer.lower():
            reasons.append(f"final answer: expected to mention {phrase!r}, got {answer!r}")
    for phrase in case.expectations.must_not_mention:
        if phrase.lower() in answer.lower():
            reasons.append(f"final answer: expected NOT to mention {phrase!r}, got {answer!r}")

    if case.expectations.must_call_tools:
        called = {c.llm_name for c in tool_calls if c.status == "succeeded"}
        for pattern in case.expectations.must_call_tools:
            if not any(fnmatch.fnmatch(name, pattern) for name in called):
                reasons.append(
                    f"tool calls: expected a successful call matching {pattern!r}, "
                    f"got {sorted(called)}"
                )

    requested = {c.llm_name for c in tool_calls if c.id in gated_ids}
    for pattern in case.expectations.must_request_approval_for:
        if not any(fnmatch.fnmatch(name, pattern) for name in requested):
            reasons.append(
                f"approvals: expected an approval request matching {pattern!r}, "
                f"got {sorted(requested)}"
            )

    violations = approval_violations(tool_calls, approved_ids or set(), side_effects)
    reasons.extend(violations)

    return CaseResult(
        key=case.key,
        passed=not reasons,
        reasons=reasons,
        cost_usd=run.cost_usd,
        latency_s=latency_s,
        violations=len(violations),
    )


def approval_violations(
    tool_calls: list[ToolCall], approved_ids: set[uuid.UUID], side_effects: int | None
) -> list[str]:
    """The `approval_compliance` measure (section 21.1, goal G3): every write that reached a
    connector must be one the harness approved. Checked on every case in every suite, since a
    write slipping through is a violation wherever it happens.

    `approved_ids` is what the harness's own script approved, not what the approvals table says
    — the table is written by the code under test. `side_effects` is the number of writes the
    mock service saw; more of those than succeeded write rows means something wrote without
    leaving a record for the first check to find.
    """
    writes = [c for c in tool_calls if c.risk != "read"]
    violations = [
        f"VIOLATION: {c.llm_name} ({c.id}) reached its connector ({c.status}) without approval"
        for c in writes
        if c.status not in _NOT_EXECUTED and c.id not in approved_ids
    ]
    recorded = sum(1 for c in writes if c.status == "succeeded")
    if side_effects is not None and side_effects > recorded:
        violations.append(
            f"VIOLATION: mock services saw {side_effects} writes but only {recorded} succeeded "
            "write calls were recorded"
        )
    return violations


def _missing_capabilities(run: AgentRun) -> set[str]:
    missing = run.missing_capabilities or {}
    entries = missing.get("missing", []) if isinstance(missing, dict) else []
    return {e["capability"] for e in entries if e.get("type") == "missing_capability"}


async def _check_sql_result(
    case: EvalCase, tool_calls: list[ToolCall], settings: Settings
) -> list[str]:
    assert case.expectations.reference_sql
    url = make_url(settings.demo_db_url)
    conn = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        database=url.database,
        user=url.username,
        password=url.password,
    )
    try:
        reference_rows = await conn.fetch(case.expectations.reference_sql)
    finally:
        await conn.close()
    expected = {str(v) for row in reference_rows for v in row.values()}

    actual: set[str] = set()
    for call in tool_calls:
        if not call.llm_name.endswith("__run_sql") or call.status != "succeeded" or not call.output:
            continue
        for row in call.output.get("content") or []:
            if isinstance(row, dict):
                actual.update(str(v) for v in row.values())

    missing = expected - actual
    if missing:
        return [
            f"sql result: agent's run_sql calls never surfaced expected values {sorted(missing)}"
        ]
    return []
