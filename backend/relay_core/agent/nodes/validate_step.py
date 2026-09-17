"""`validate_step` (docs/system-design.md sections 8.5, 8.8): checks whether the step that just
ran actually met its `expected_output`.

A verdict of `"replan"` is the single entry to `relay_core.agent.nodes.replan` (ADR-0013
decision 4): the step is still marked `failed`, and `replan_reason` is what tells the graph to
revise the plan rather than carry the failure forward. Past `Replan.MAX_REPLANS` the verdict
degrades to `"fail"` — `next_step`'s cascading skip then honestly skips whatever depended on
the step, and `synthesize` explains the gap in the final answer.

Retries reset the step back to `pending` rather than routing straight to `execute_step`: the
conditional edge in `relay_core.agent.graph` reads the step's status off the state
`validate_step` returns to decide where to go next, so "retry" *is* "still pending" from the
graph's point of view.
"""

from typing import Any, Literal

from pydantic import BaseModel

from relay_core.agent.deps import AgentDeps
from relay_core.agent.nodes.replan import MAX_REPLANS
from relay_core.agent.state import AgentState, update_step
from relay_core.llm.profiles import VALIDATOR
from relay_core.llm.schemas import parse_structured

_MAX_ATTEMPTS = 2

_SYSTEM_PROMPT = """\
You are validating one completed step of a plan for Relay, an operations agent.

Step goal: {goal}
Done means: {expected_output}
What happened: {result_summary}

Judge only from "What happened" above — don't assume anything the step didn't report. Return
"pass" if it satisfies "Done means". Return "retry" if this looks like a fixable, transient
problem (e.g. a tool error worth trying again). Return "replan" if this step cannot work as
written but the objective could still be reached another way. Return "fail" if it's
fundamentally blocked (missing data, contradictory results, nothing any plan would fix).

Return JSON matching the schema. `reason` should be one sentence a user could read.
"""


class StepVerdict(BaseModel):
    status: Literal["pass", "retry", "replan", "fail"]
    reason: str
    issues: list[str] = []
    suggested_fix: str | None = None


class ValidateStep:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        plan = state.plan
        assert plan is not None and state.current_step_id is not None, (
            "validate_step reached with no plan or no current step"
        )
        step = next(s for s in plan.steps if s.id == state.current_step_id)

        if step.status == "failed":
            # execute_step already gave up (budget exhausted / didn't converge) — nothing to
            # validate, and there's no LLM call worth spending on a step we know failed.
            return {}

        resp = await self.deps.gateway.generate(
            role=VALIDATOR,
            system=_SYSTEM_PROMPT.format(
                goal=step.goal,
                expected_output=step.expected_output,
                result_summary=step.result_summary or "(no summary — the step produced nothing)",
            ),
            contents="Validate this step's result.",
            workspace_id=state.workspace_id,
            run_id=state.run_id,
            response_schema=StepVerdict,
            settings=self.deps.settings,
        )
        verdict = parse_structured(resp, StepVerdict)

        if verdict is not None and verdict.status == "pass":
            return {}
        if verdict is not None and verdict.status == "retry" and step.attempts < _MAX_ATTEMPTS:
            return {"plan": update_step(plan, step.id, status="pending")}

        reason = verdict.reason if verdict is not None else "Could not validate this step's result."
        failed = {"plan": update_step(plan, step.id, status="failed", result_summary=reason)}
        if (
            verdict is not None
            and verdict.status == "replan"
            and state.replans_used < MAX_REPLANS
        ):
            return failed | {"replan_reason": reason}
        return failed
