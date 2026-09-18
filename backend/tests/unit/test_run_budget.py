"""Phase 8 B1: `enforce_budget`'s limits, including the wall clock that `approval_gate` resets
on resume so time parked waiting for a human is not counted (docs/system-design.md 19.2)."""

import time

import pytest

from relay_core.agent.nodes.execute_step import BudgetExceeded, enforce_budget
from relay_core.agent.state import Budget


def test_each_limit_raises_and_a_fresh_budget_does_not() -> None:
    enforce_budget(Budget(clock_started_at=time.monotonic()))
    for used in (
        {"used_tool_calls": 40},
        {"used_llm_calls": 60},
        {"used_cost_usd": 0.5},
        {"clock_started_at": time.monotonic() - 301},
    ):
        with pytest.raises(BudgetExceeded):
            enforce_budget(Budget(**used))


def test_a_reset_clock_forgets_time_parked_on_an_approval() -> None:
    parked = Budget(clock_started_at=time.monotonic() - 3600)
    with pytest.raises(BudgetExceeded, match="Wall-time"):
        enforce_budget(parked)
    enforce_budget(parked.model_copy(update={"clock_started_at": time.monotonic()}))
