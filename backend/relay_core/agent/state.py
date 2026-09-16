"""Graph state (docs/system-design.md section 8.3). `pending_approval_id` is still deferred —
`approval_gate` doesn't exist until Phase 5, and none of Phase 3's built-in tools are ever
`write`/`destructive` (postgres and file_upload are read-only), so there's nothing yet that
would set it.
"""

import uuid
from operator import add, or_
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    id: str
    goal: str
    required_capabilities: list[str] = Field(default_factory=list)
    optional_capabilities: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    expected_output: str
    status: Literal[
        "pending", "running", "done", "failed", "skipped", "blocked_missing_capability"
    ] = "pending"
    result_summary: str | None = None
    attempts: int = 0


class Plan(BaseModel):
    objective: str
    steps: list[PlanStep]
    assumptions: list[str] = Field(default_factory=list)
    needs_clarification: str | None = None


class Budget(BaseModel):
    """Bounds `execute_step`'s tool-calling loop across the whole run (section 8.3). Only
    `max_tool_calls`/`used_tool_calls` are enforced this phase (`relay_core.agent.nodes.
    execute_step.enforce_budget`) — the rest of the shape matches the design now so a later
    phase's real per-workspace budgets (section 19, `workspace_policies.run_budget`) slot into
    the same field names instead of renaming a checkpointed state shape.
    """

    max_steps: int = 10
    max_tool_calls: int = 40
    max_llm_calls: int = 60
    max_cost_usd: float = 0.50
    max_wall_seconds: int = 300
    used_tool_calls: int = 0
    used_llm_calls: int = 0
    used_cost_usd: float = 0.0


class AgentState(BaseModel):
    # identity
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    trigger_message_id: uuid.UUID

    # input & context
    user_message: str
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)
    history_summary: str | None = None
    available_capabilities: list[str] = Field(default_factory=list)

    # routing & planning
    route: Literal["direct", "task", "blocked"] | None = None
    plan: Plan | None = None
    missing: list[dict[str, Any]] = Field(default_factory=list)

    # execution (relay_core.agent.nodes.execute_step / validate_step / next_step / synthesize)
    current_step_id: str | None = None
    # No `scratchpad` field: section 8.3's design keeps the Gemini `Content` turns for the
    # in-progress step in graph state so they survive an `approval_gate` interrupt and resume
    # exactly where they left off. Phase 3 has no interrupts — every built-in tool this phase
    # ships is `read` risk, so `execute_step` never pauses mid-step — so the whole bounded
    # ReAct loop runs inside one node call and keeps its turns in a local variable instead.
    # Add it back here when Phase 5's approval gate needs a step to survive across invocations.
    step_outputs: Annotated[dict[str, str], or_] = Field(default_factory=dict)
    sources: Annotated[list[dict[str, Any]], add] = Field(default_factory=list)
    artifacts: Annotated[list[dict[str, Any]], add] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)

    # output
    final_answer: str | None = None
    error: str | None = None


def update_step(plan: Plan, step_id: str, **fields: Any) -> Plan:
    """Returns a new `Plan` with one step replaced by a copy carrying `fields`. `plan` has no
    reducer (whole-object replace, like every other non-`Annotated` field on `AgentState`), so
    every node that touches a step's status/result goes through this instead of mutating a
    `PlanStep` in place — the plan objects handed to a node are shared with whatever LangGraph
    kept from the previous step, and mutating them risks corrupting that history.
    """
    steps = [s.model_copy(update=fields) if s.id == step_id else s for s in plan.steps]
    return plan.model_copy(update={"steps": steps})
