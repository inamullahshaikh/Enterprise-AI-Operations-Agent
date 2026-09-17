"""Eval case schema (docs/system-design.md section 21.3), trimmed to the fields the suites built
so far actually check. The full case format also covers injection/config_matrix expectations —
those land with the suites that read them.

`depends_on_case` is Phase 7's addition and the only one that changes how a suite *runs* rather
than how it is scored: a memory case needs two runs in order, the first stating a preference and
the second expected to honour it. Ordering the cases is enough, because every case in a profile
already shares one workspace and memory is scoped to the workspace and user, not the
conversation — so a second runner would be machinery for nothing.
"""

from collections import deque
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Expectations(BaseModel):
    route: Literal["direct", "task", "blocked"] | None = None
    missing_capabilities: list[str] = Field(default_factory=list)
    expect_no_missing_capabilities: bool = False
    # A reference query returning the values (any number of columns) the agent's own SQL
    # should have surfaced somewhere — scored by value membership, not column-name equality,
    # since the agent writes its own SQL and may alias columns differently than the reference
    # (relay_eval.scoring._check_sql_result).
    reference_sql: str | None = None
    max_cost_usd: float | None = None
    # The `rag` suite (section 21.1's recall@k/faithfulness/citation-accuracy row, trimmed to
    # deterministic checks per docs/adr/0009 — no LLM-as-judge yet): substrings the final answer
    # must/must-not contain, and glob patterns (fnmatch against `llm_name`, e.g.
    # "*__search_documents") the run must have called at least one successful tool call for.
    must_mention: list[str] = Field(default_factory=list)
    must_not_mention: list[str] = Field(default_factory=list)
    must_call_tools: list[str] = Field(default_factory=list)
    # Write cases (Phase 5): glob patterns that must each match at least one call the run put up
    # for approval, and how the harness answers every approval the run raises. `approve_first`
    # approves only the first item of each approval, exercising partial batches (section 13.4).
    must_request_approval_for: list[str] = Field(default_factory=list)
    approval_decision: Literal["approve_all", "approve_first", "reject_all"] = "approve_all"
    # The `planning` suite (Phase 7 F1): the first plan cannot work, and the run is expected to
    # revise it rather than report a dead end. Counted from `llm_calls` rows, where `plan` and
    # `replan` share the `planner` role — so more than one planner call means the plan changed.
    expect_replan: bool = False


class EvalCase(BaseModel):
    key: str
    suite: str
    connector_profile: Literal["none", "db_only", "csv_only", "docs_only", "full"] = "none"
    message: str
    # Relative to evals/fixtures/ — only used when connector_profile is csv_only.
    csv_fixture: str | None = None
    # Run this case only after the named case, in the same workspace. What the first run
    # remembers is what the second is scored on.
    depends_on_case: str | None = None
    expectations: Expectations
    tags: list[str] = Field(default_factory=list)


def load_suite(suites_dir: Path, suite: str) -> list[EvalCase]:
    cases = []
    for path in sorted((suites_dir / suite).glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        cases.append(EvalCase.model_validate(raw))
    return order_cases(cases)


def order_cases(cases: list[EvalCase]) -> list[EvalCase]:
    """Filename order, except that a case runs after the case it depends on. Raises on a cycle
    or a missing dependency rather than silently dropping a case — a memory case that never ran
    its setup case would fail for a reason that has nothing to do with memory."""
    by_key = {c.key: c for c in cases}
    pending = deque(cases)
    ordered: list[EvalCase] = []
    placed: set[str] = set()

    for case in cases:
        if case.depends_on_case and case.depends_on_case not in by_key:
            raise ValueError(
                f"case {case.key!r} depends_on_case {case.depends_on_case!r}, "
                "which is not in this suite"
            )

    stalled = 0
    while pending:
        case = pending.popleft()
        if case.depends_on_case is None or case.depends_on_case in placed:
            ordered.append(case)
            placed.add(case.key)
            stalled = 0
            continue
        pending.append(case)
        stalled += 1
        if stalled > len(pending):
            cycle = sorted(c.key for c in pending)
            raise ValueError(f"depends_on_case cycle among {cycle}")
    return ordered
