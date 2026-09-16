"""Deterministic scoring for the suites built so far (docs/system-design.md section 21.4's
"deterministic checks first" — LLM-as-judge scoring, needed for `planning`/`task_success` and
for `capability_detection`'s own `fabrication_check`, is Phase 8, once there's enough case
history to calibrate a judge against, per docs/adr/0009). The `rag` suite's `must_mention` /
`must_call_tools` checks below are deterministic in that same spirit — substring and tool-name
matching, not a judge scoring faithfulness.
"""

import fnmatch
from dataclasses import dataclass, field
from decimal import Decimal

import asyncpg
from relay_core.config import Settings
from relay_core.db.models.runs import AgentRun
from relay_core.db.models.tool_calls import ToolCall
from sqlalchemy.engine import make_url

from relay_eval.cases import EvalCase


@dataclass
class CaseResult:
    key: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    cost_usd: Decimal = Decimal(0)
    latency_s: float = 0.0


async def score_case(
    case: EvalCase,
    run: AgentRun,
    tool_calls: list[ToolCall],
    settings: Settings,
    latency_s: float,
    final_answer: str | None = None,
) -> CaseResult:
    reasons: list[str] = []

    if case.expectations.route is not None and run.route != case.expectations.route:
        reasons.append(f"route: expected {case.expectations.route!r}, got {run.route!r}")

    actual_missing = _missing_capabilities(run)
    if case.expectations.missing_capabilities:
        expected = set(case.expectations.missing_capabilities)
        if not expected <= actual_missing:
            reasons.append(
                f"missing_capabilities: expected {sorted(expected)} to be a subset of {sorted(actual_missing)}"
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

    return CaseResult(
        key=case.key,
        passed=not reasons,
        reasons=reasons,
        cost_usd=run.cost_usd,
        latency_s=latency_s,
    )


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
