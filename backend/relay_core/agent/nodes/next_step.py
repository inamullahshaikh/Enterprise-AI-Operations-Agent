"""`next_step` (docs/system-design.md section 8.5): picks the next plan step whose dependencies
are already `done`, or clears `current_step_id` so the graph's conditional edge routes to
`synthesize` once nothing is left to run.

Also the one place that re-persists `agent_runs.plan` as steps execute: `plan` (the node)
writes it once, right after planning, and nothing after that touches the DB row again unless
this does — `execute_step`/`validate_step` only update the in-memory LangGraph state, which is
exactly what drives step selection but is *not* what `GET /runs/{id}` reads. Every call here
(including the final one, when nothing's left to run) writes the current plan back so a
step's real status/`result_summary` is visible from outside the run before `finalize` ever
runs — useful on its own for progress polling, and once `run.plan` is JSON either way, one
`set_plan` call per `next_step` invocation is simpler than every node that touches step status
calling it itself.

Also cascades failure: a pending step that depends on one that ended `failed`/`skipped`/
`blocked_missing_capability` can never run either, so it's marked `skipped` here rather than
sitting `pending` forever in the persisted plan — there's no `replan` node yet (Phase 7) to
route around it instead.
"""

from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState, PlanStep, update_step
from relay_core.events.types import STEP_FINISHED, STEP_STARTED

_TERMINAL_NOT_DONE = {"failed", "skipped", "blocked_missing_capability"}
_JUST_FINISHED = {"done", "failed"}


class NextStep:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        plan = state.plan
        assert plan is not None, "next_step reached with no plan"

        if state.current_step_id is not None:
            finished = next((s for s in plan.steps if s.id == state.current_step_id), None)
            if finished is not None and finished.status in _JUST_FINISHED:
                await self.deps.events.publish(
                    state.run_id,
                    STEP_FINISHED,
                    {
                        "step_id": finished.id,
                        "status": finished.status,
                        "summary": finished.result_summary,
                    },
                )

        status_by_id = {s.id: s.status for s in plan.steps}
        for step in plan.steps:
            if step.status == "pending" and any(
                status_by_id.get(dep) in _TERMINAL_NOT_DONE for dep in step.depends_on
            ):
                plan = update_step(plan, step.id, status="skipped")
                status_by_id[step.id] = "skipped"

        done_ids = {s.id for s in plan.steps if s.status == "done"}
        next_step_: PlanStep | None = next(
            (s for s in plan.steps if s.status == "pending" and set(s.depends_on) <= done_ids),
            None,
        )
        if next_step_ is not None:
            plan = update_step(plan, next_step_.id, status="running")
            await self.deps.events.publish(
                state.run_id, STEP_STARTED, {"step_id": next_step_.id, "goal": next_step_.goal}
            )

        await self.deps.runs.set_plan(
            state.workspace_id, state.run_id, plan.model_dump(mode="json")
        )
        # Clearing `scratchpad` is what keeps section 8.6's "scratchpad is per step" promise:
        # each step starts from its own prompt, and one step's Gemini turns never leak into the
        # next one's context.
        return {
            "plan": plan,
            "current_step_id": next_step_.id if next_step_ is not None else None,
            "scratchpad": [],
        }
