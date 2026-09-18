"""`plan` (docs/system-design.md sections 8.5, 8.8): produces a structured
`Plan` from the objective and the capability taxonomy. The planner is told
what's available in the workspace (`available_capabilities`, always `[]`
before Phase 3's connectors exist) but plans against the full taxonomy
regardless — `check_capabilities` is what turns "planned but not installed"
into an honest gap for the user, not the planner itself.
"""

from typing import Any

from relay_core.agent.context import remembered_context
from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState, Plan, PlanStep
from relay_core.capabilities.taxonomy import render_catalog
from relay_core.events.types import PLAN_CREATED
from relay_core.llm.profiles import PLANNER
from relay_core.llm.schemas import parse_structured

_MAX_STEPS = 10

_SYSTEM_PROMPT = """\
You are the planner for Relay, an operations agent inside a company workspace.

Break the user's objective into the smallest sequence of steps that achieves it.
For each step, list the capabilities it REQUIRES from this list only:
{capability_catalog}

Available in this workspace right now: {available_capabilities}

Rules:
- Use only capabilities from the catalog above. Do not invent capabilities.
- If a required capability is not available, still include the step and list it;
  the system will ask the user how to proceed. Never pretend data exists.
- Any step that sends, creates, updates, or deletes something must be its own step.
- If the objective is ambiguous in a way that changes the result, set
  needs_clarification to ONE question instead of planning.
- Maximum {max_steps} steps.
Return JSON matching the schema.
"""


class PlanNode:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        if self.deps.settings.experiment_single_react:
            # Experiment 1 (section 21.6): no planner. One step whose goal is the whole objective
            # and which may use anything available *is* a single ReAct loop, on the same
            # executor, tools and validators as the real path.
            plan = Plan(
                objective=state.user_message,
                steps=[
                    PlanStep(
                        id="s1",
                        goal=state.user_message,
                        optional_capabilities=state.available_capabilities,
                        expected_output="A complete answer to the objective",
                    )
                ],
            )
            await self.deps.runs.set_plan(
                state.workspace_id, state.run_id, plan.model_dump(mode="json")
            )
            return {"plan": plan}

        # Custom capabilities (`custom.*`, from MCP/OpenAPI tool tagging) have no taxonomy entry,
        # so the planner could never require one unless the workspace's own are listed too.
        custom = [c for c in state.available_capabilities if c.startswith("custom.")]
        catalog = "\n".join([render_catalog(), *(f"{c} — Workspace-specific" for c in custom)])
        system = _SYSTEM_PROMPT.format(
            capability_catalog=catalog,
            available_capabilities=", ".join(state.available_capabilities) or "(none)",
            max_steps=_MAX_STEPS,
        ) + remembered_context(state)
        resp = await self.deps.gateway.generate(
            role=PLANNER,
            system=system,
            contents=state.user_message,
            workspace_id=state.workspace_id,
            run_id=state.run_id,
            response_schema=Plan,
            settings=self.deps.settings,
        )
        plan = parse_structured(resp, Plan) or Plan(objective=state.user_message, steps=[])
        await self.deps.runs.set_plan(
            state.workspace_id, state.run_id, plan.model_dump(mode="json")
        )
        await self.deps.events.publish(
            state.run_id,
            PLAN_CREATED,
            {"objective": plan.objective, "steps": [s.model_dump(mode="json") for s in plan.steps]},
        )
        return {"plan": plan}
