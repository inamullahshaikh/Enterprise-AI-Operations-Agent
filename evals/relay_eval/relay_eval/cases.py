"""Eval case schema (docs/system-design.md section 21.3), trimmed to the fields the three
Phase 3 suites (routing, capability_detection, text_to_sql) actually check. The full case
format also covers tool_selection/rag/task_success/approval_compliance/injection/config_matrix
expectations (`must_call_tools`, `approval_decision`, artifact checks, ...) — those land with
the suites that read them.
"""

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


class EvalCase(BaseModel):
    key: str
    suite: str
    connector_profile: Literal["none", "db_only", "csv_only"] = "none"
    message: str
    # Relative to evals/fixtures/ — only used when connector_profile is csv_only.
    csv_fixture: str | None = None
    expectations: Expectations
    tags: list[str] = Field(default_factory=list)


def load_suite(suites_dir: Path, suite: str) -> list[EvalCase]:
    cases = []
    for path in sorted((suites_dir / suite).glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        cases.append(EvalCase.model_validate(raw))
    return cases
