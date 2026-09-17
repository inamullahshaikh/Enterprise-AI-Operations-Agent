"""`replan` (docs/system-design.md sections 8.2, 8.5): the step that failed didn't have to end
the run. Given what has already been achieved and why the current step is unworkable, the
planner writes the rest of the plan again.

**`validate_step` is the only entry.** Section 8.2 also draws `approval_gate --> replan`, but a
rejected approval has to come back to the model as a function response for the turn to be valid
at all (ADR-0011 decision 5), and the step's own validator is a better judge than the gate of
whether the plan is now unworkable. So a rejection is summarized into the step result like any
other outcome, and if that genuinely breaks the plan, the validator says `replan` and we arrive
here.

**Completed steps are preserved in code, not by asking nicely.** The prompt says to keep them,
but a model that renumbers or rewrites a finished step would silently erase results the answer
is supposed to rest on, so anything already `done` is carried over verbatim and only
`pending`/`failed` steps can be replaced.

**Bounded at `_MAX_REPLANS`.** At the bound `validate_step` stops asking for a revision and the
run synthesizes what it has: a truthful partial answer beats a loop that spends a budget
rediscovering the same dead end.
"""

from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState, Plan
from relay_core.capabilities.taxonomy import render_catalog
from relay_core.events.types import PLAN_UPDATED
from relay_core.llm.profiles import PLANNER
from relay_core.llm.schemas import parse_structured

# Two revisions, then the run answers with what it has. Read by `validate_step`, which is what
# decides whether a `replan` verdict routes here at all.
MAX_REPLANS = 2

_SYSTEM_PROMPT = """\
You are re-planning for Relay, an operations agent inside a company workspace. An earlier plan
ran into a step that cannot work, and you are writing the rest of the plan again.

Objective: {objective}

The plan so far, with what each step produced:
{plan_so_far}

Why the plan needs revising: {reason}

Capabilities you may require, from this list only:
{capability_catalog}

Available in this workspace right now: {available_capabilities}

Rules:
- Steps already marked [done] are finished. Repeat them unchanged, with the same ids, and build
  on what they produced. Never re-run work that already succeeded.
- Replace the failed step and anything still pending with steps that route around the problem.
- If nothing can route around it, return the done steps alone. A shorter honest plan is better
  than a step you expect to fail.
- Do not invent capabilities, and do not pretend data exists.
- Any step that sends, creates, updates, or deletes something must be its own step.
- Maximum {max_steps} steps.
Return JSON matching the schema.
"""

_MAX_STEPS = 10


class Replan:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        plan = state.plan
        assert plan is not None, "replan reached with no plan"

        custom = [c for c in state.available_capabilities if c.startswith("custom.")]
        catalog = "\n".join([render_catalog(), *(f"{c} — Workspace-specific" for c in custom)])
        resp = await self.deps.gateway.generate(
            role=PLANNER,
            system=_SYSTEM_PROMPT.format(
                objective=plan.objective,
                plan_so_far="\n".join(
                    f"[{s.status}] {s.id}: {s.goal} -> {s.result_summary or '(no result yet)'}"
                    for s in plan.steps
                ),
                reason=state.replan_reason or "A step failed.",
                capability_catalog=catalog,
                available_capabilities=", ".join(state.available_capabilities) or "(none)",
                max_steps=_MAX_STEPS,
            ),
            contents="Revise the plan.",
            workspace_id=state.workspace_id,
            run_id=state.run_id,
            response_schema=Plan,
            settings=self.deps.settings,
        )
        revision = parse_structured(resp, Plan)
        revised = _merge(plan, revision)

        if revised is None:
            # A parse failure or a revision that adds nothing is a failure, not a retry: the
            # step stays `failed`, `next_step` cascades the skip, and `synthesize` explains the
            # gap. Exactly what happened before this node existed.
            return {"replan_reason": None, "replans_used": state.replans_used + 1}

        await self.deps.runs.set_plan(
            state.workspace_id, state.run_id, revised.model_dump(mode="json")
        )
        await self.deps.events.publish(
            state.run_id,
            PLAN_UPDATED,
            {
                "objective": revised.objective,
                "reason": state.replan_reason,
                "steps": [s.model_dump(mode="json") for s in revised.steps],
            },
        )
        return {
            "plan": revised,
            "replans_used": state.replans_used + 1,
            "replan_reason": None,
            # The failed step is gone; nothing is current until `next_step` picks one, and its
            # scratchpad must not leak into whatever runs next.
            "current_step_id": None,
            "scratchpad": [],
        }


def _merge(plan: Plan, revision: Plan | None) -> Plan | None:
    """Finished steps verbatim, then whatever the revision adds. `None` means the revision was
    unusable and the run should carry on with the plan it already has."""
    if revision is None:
        return None
    kept = [s for s in plan.steps if s.status == "done"]
    kept_ids = {s.id for s in kept}
    added = [
        s.model_copy(update={"status": "pending", "attempts": 0, "result_summary": None})
        for s in revision.steps
        if s.id not in kept_ids
    ][:_MAX_STEPS]
    if not added:
        return None
    # A revision often points a new step at the id of the step that just failed, which is no
    # longer in the plan. `next_step` only runs a step once every id it depends on is `done`, so
    # a dangling dependency would quietly park the step forever.
    live = kept_ids | {s.id for s in added}
    added = [
        s.model_copy(update={"depends_on": [d for d in s.depends_on if d in live]}) for s in added
    ]
    return plan.model_copy(
        update={
            "steps": [*kept, *added],
            "assumptions": revision.assumptions,
            "needs_clarification": None,
        }
    )
