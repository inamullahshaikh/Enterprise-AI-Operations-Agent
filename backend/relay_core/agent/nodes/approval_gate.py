"""`approval_gate` (docs/system-design.md section 13.2): the point where a run stops and waits
for a human.

`interrupt()` suspends the graph here. LangGraph checkpoints the state, `ainvoke` raises out of
the worker, and the run sits at `awaiting_approval` until
`relay_core.agent.runner.resume_agent_once` re-invokes the same thread with a
`Command(resume=...)` carrying the decision. Resuming re-enters *this node from the top*, so
everything above the `interrupt()` call runs a second time — which is why the approval row and
its `tool_calls` rows are created back in `execute_step`, and why the only things left here are
a status write and an event publish, both safe to repeat.

**Answering the turn.** The proposed calls are read straight off the last model turn in the
scratchpad rather than from any extra state: that turn is already checkpointed, and Gemini needs
a function response for every function call it contains. So this node answers all of them at
once — executing the approved writes, executing the read calls that were held alongside them,
and returning an explicit "rejected by <user>" response for anything the approver declined. The
model sees a complete, truthful account of what happened and can react to it (usually by
summarizing the step without the rejected action), which is why a rejection does not simply
fail the step.

Classification has to match `execute_step` exactly — the same `requires_approval` predicate over
the same policy — because that's what lines the gated calls up with the approval's
`tool_call_ids`, in order.
"""

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from google.genai import types
from langgraph.types import interrupt

from relay_core.agent.deps import AgentDeps
from relay_core.agent.nodes.execute_step import (
    build_function_response_content,
    requires_approval,
)
from relay_core.agent.scratchpad import dump_contents, load_contents
from relay_core.agent.state import AgentState
from relay_core.connectors.base import ToolResult
from relay_core.db.models.approvals import Approval
from relay_core.events.types import APPROVAL_DECIDED
from relay_core.policy import ApprovalRules
from relay_core.tools.registry import BoundTool, BoundToolSet


@dataclass(frozen=True)
class _PlannedCall:
    """One function call from the model turn, paired with the `tool_calls` row it needs a
    decision for — `row_id` is None for calls that were never gated."""

    call: types.FunctionCall
    bound: BoundTool | None
    row_id: uuid.UUID | None


class ApprovalGate:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        assert state.pending_approval_id is not None, "approval_gate reached with no approval"
        approval = await self.deps.approvals.get(state.workspace_id, state.pending_approval_id)
        assert approval is not None, "approval_gate reached with an approval that doesn't exist"

        await self.deps.runs.mark_awaiting_approval(state.workspace_id, state.run_id)

        # Everything above re-runs on resume; everything below sees the decision.
        decision: dict[str, Any] = interrupt({"approval_id": str(approval.id)})

        approved_ids = _approved_tool_call_ids(approval, decision)
        contents = load_contents(state.scratchpad)
        calls = _function_calls_of_last_model_turn(contents)

        # Bound with the *same* capability filter `execute_step` used, so the tool set — and
        # therefore the risk each call resolves to — is identical on both sides of the interrupt.
        tools = await self.deps.tool_registry.tools_for_run(
            workspace_id=state.workspace_id,
            user_id=state.user_id,
            run_id=state.run_id,
            conversation_id=state.conversation_id,
            capabilities=_step_capabilities(state),
        )
        rules = await self.deps.policies.approval_rules(state.workspace_id)

        results, executed = await self._answer_turn(
            state, approval, decision, calls, tools, rules, approved_ids
        )
        contents.append(build_function_response_content(calls, results))

        await self.deps.events.publish(
            state.run_id,
            APPROVAL_DECIDED,
            {"approval_id": str(approval.id), "status": approval.status},
        )
        await self.deps.runs.mark_running(state.workspace_id, state.run_id)

        return {
            "scratchpad": dump_contents(contents),
            "pending_approval_id": None,
            "budget": state.budget.model_copy(
                update={"used_tool_calls": state.budget.used_tool_calls + executed}
            ),
        }

    async def _answer_turn(
        self,
        state: AgentState,
        approval: Approval,
        decision: dict[str, Any],
        calls: list[types.FunctionCall],
        tools: BoundToolSet,
        rules: ApprovalRules,
        approved_ids: set[uuid.UUID],
    ) -> tuple[list[ToolResult], int]:
        """Produces one result per call, in the model turn's own order.

        Gated calls are matched to their `tool_calls` row positionally: `execute_step` created
        the rows in the order the gated calls appeared, and this walks the same turn with the
        same predicate, so the n-th gated call here is the n-th row there.
        """
        planned: list[_PlannedCall] = []
        gated_index = 0
        for call in calls:
            bound = tools.lookup(call.name or "")
            row_id: uuid.UUID | None = None
            if requires_approval(bound, call, rules, state.user_role):
                row_id = approval.tool_call_ids[gated_index]
                gated_index += 1
            planned.append(_PlannedCall(call=call, bound=bound, row_id=row_id))

        resolved = await asyncio.gather(
            *[self._run_one(state, approval, decision, p, approved_ids) for p in planned]
        )
        return [r for r, _ in resolved], sum(1 for _, ran in resolved if ran)

    async def _run_one(
        self,
        state: AgentState,
        approval: Approval,
        decision: dict[str, Any],
        planned: _PlannedCall,
        approved_ids: set[uuid.UUID],
    ) -> tuple[ToolResult, bool]:
        call, bound, row_id = planned.call, planned.bound, planned.row_id
        if bound is None:
            return ToolResult(ok=False, error=f"Unknown tool {call.name!r}"), False

        reason = decision.get("reason")
        if row_id is not None and row_id not in approved_ids:
            await self.deps.tool_calls.mark_not_executed(
                state.workspace_id,
                row_id,
                status="rejected" if approval.status == "rejected" else "skipped",
                reason=reason,
            )
            return ToolResult(ok=False, error=_rejection_message(reason)), False

        args = _args_for(call, decision, row_id) if row_id is not None else dict(call.args or {})
        result = await self.deps.tool_executor.run(
            workspace_id=state.workspace_id,
            run_id=state.run_id,
            plan_step_id=state.current_step_id or "",
            bound=bound,
            args=args,
            tool_call_id=row_id,
        )
        return result, True


def _approved_tool_call_ids(approval: Approval, decision: dict[str, Any]) -> set[uuid.UUID]:
    """Which of the approval's calls the decision actually cleared. `item_ids` lets an approver
    tick part of a batch (section 13.4); omitting it means the whole batch."""
    if decision.get("action") != "approve":
        return set()
    item_ids = decision.get("item_ids")
    if not item_ids:
        return set(approval.tool_call_ids)
    wanted = {str(i) for i in item_ids}
    return {row_id for row_id in approval.tool_call_ids if str(row_id) in wanted}


def _args_for(
    call: types.FunctionCall, decision: dict[str, Any], row_id: uuid.UUID
) -> dict[str, Any]:
    """Approvers can edit arguments before approving (FR-14). Edits are keyed by `tool_calls`
    row id so a batch can be corrected item by item."""
    edited = (decision.get("edited_args") or {}).get(str(row_id))
    return dict(edited) if edited else dict(call.args or {})


def _rejection_message(reason: str | None) -> str:
    base = "A human reviewer declined this action, so it was not performed."
    return f"{base} Reason: {reason}" if reason else base


def _step_capabilities(state: AgentState) -> list[str]:
    assert state.plan is not None and state.current_step_id is not None
    step = next(s for s in state.plan.steps if s.id == state.current_step_id)
    return step.required_capabilities + step.optional_capabilities


def _function_calls_of_last_model_turn(
    contents: list[types.Content],
) -> list[types.FunctionCall]:
    for content in reversed(contents):
        calls = [p.function_call for p in (content.parts or []) if p.function_call is not None]
        if calls:
            return calls
    raise ValueError("approval_gate resumed with no function calls in the scratchpad")
