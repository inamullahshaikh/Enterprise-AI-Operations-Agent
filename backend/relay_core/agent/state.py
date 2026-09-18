"""Graph state (docs/system-design.md section 8.3).

Everything here is checkpointed, which makes the field shapes a compatibility surface: a run
parked on `awaiting_approval` is resumed by a *different* worker process, reading state this
process wrote. Phase 5's additions — `scratchpad` and `pending_approval_id` — are what make that
resume possible, and `relay_core.agent.scratchpad` explains why the former is stored as
serialized dicts rather than the Gemini SDK's own `Content` objects.
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
    """Per-run limits (sections 8.3, 19.2), loaded from `workspace_policies.run_budget` by
    `load_context` and enforced by `relay_core.agent.nodes.execute_step.enforce_budget`.
    `used_llm_calls`/`used_cost_usd` are refreshed from `llm_calls` before each check, so every
    node's spend counts without each node reporting it. `clock_started_at` is `time.monotonic()`
    in the worker process currently running the graph: `load_context` sets it and `approval_gate`
    resets it on resume, so time parked waiting for a human is not the run's spend.
    """

    max_steps: int = 10
    max_tool_calls: int = 40
    max_llm_calls: int = 60
    max_cost_usd: float = 0.50
    max_wall_seconds: int = 300
    used_tool_calls: int = 0
    used_llm_calls: int = 0
    used_cost_usd: float = 0.0
    clock_started_at: float | None = None


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
    # What the agent remembers about this user and workspace (section 12.3), already rendered
    # to strings by `load_context`. Rows would be a compatibility surface on a checkpointed
    # state for no benefit — nothing downstream needs a memory's id, only its text.
    memories: list[str] = Field(default_factory=list)
    available_capabilities: list[str] = Field(default_factory=list)
    # The triggering user's workspace role, snapshotted by `load_context`. Feeds section 13.1's
    # approval rules, where a `never` rule only waives approval for owners and admins. Held in
    # state rather than re-read per call so a mid-run membership change can't flip the approval
    # decision for a run that's already executing.
    user_role: str = "member"

    # routing & planning
    route: Literal["direct", "task", "blocked"] | None = None
    plan: Plan | None = None
    missing: list[dict[str, Any]] = Field(default_factory=list)

    # execution (relay_core.agent.nodes.execute_step / validate_step / next_step / synthesize)
    current_step_id: str | None = None
    # Section 8.3's per-step Gemini turns, as serialized dicts rather than `types.Content` —
    # see `relay_core.agent.scratchpad` for why, and for the thought-signature constraint that
    # makes the encoding load-bearing. Whole-object replace, not a reducer: `execute_step`
    # rewrites the list each time it hands control to `approval_gate`, and `next_step` clears it
    # when the step ends, so appending would leak one step's turns into the next.
    scratchpad: list[dict[str, Any]] = Field(default_factory=list)
    step_outputs: Annotated[dict[str, str], or_] = Field(default_factory=dict)
    sources: Annotated[list[dict[str, Any]], add] = Field(default_factory=list)
    artifacts: Annotated[list[dict[str, Any]], add] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)

    # approvals (relay_core.agent.nodes.approval_gate) — set by `execute_step` when a step's
    # write calls need a human, cleared once the decision has been folded into the scratchpad.
    pending_approval_id: uuid.UUID | None = None

    # replanning (relay_core.agent.nodes.replan). `replan_reason` is both the validator's
    # explanation and the routing signal: `validate_step` sets it when a revision is worth
    # asking for, the graph's edge reads it, and `replan` clears it. `replans_used` is what
    # stops a run rediscovering the same dead end forever.
    replan_reason: str | None = None
    replans_used: int = 0

    # Set by `execute_step` when the run budget ran out (section 19.2). `next_step` skips what is
    # left and `synthesize` is told to say the answer is partial.
    budget_exhausted: str | None = None

    # The first untrusted source this run read from (section 18.4 step 5), or None. Once set,
    # every write needs approval. A string rather than a flag so the approval card can say why.
    touched_untrusted: str | None = None

    # `workspace_policies.pii_redaction`, snapshotted by `load_context`, and the placeholder ->
    # real value mapping `relay_core.security.pii` builds as tool output is redacted. Whole-object
    # replace: every node that extends it returns the full dict.
    pii_redaction: bool = False
    pii_map: dict[str, str] = Field(default_factory=dict)

    # final groundedness check (relay_core.agent.nodes.validate_final). `draft_chunks` holds the
    # streamed pieces of a draft that has not been validated yet — they are published only once
    # the draft is the one that will be finalized, so the user never watches text appear and
    # then change (ADR-0013 decision 5).
    draft_chunks: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    final_revisions: int = 0

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
