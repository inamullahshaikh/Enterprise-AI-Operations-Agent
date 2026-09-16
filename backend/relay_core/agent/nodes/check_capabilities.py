"""`check_capabilities` (docs/system-design.md sections 7.2/7.3, 8.5): resolves
every capability the plan requires against what's available, *before* any
execution starts. A pure function over state — no I/O, no LLM call.

There's no taxonomy validation here: if the planner ever invents a capability
outside the catalog, it's simply never in `available_capabilities` either, so
it's already correctly reported missing.
"""

from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState


class CheckCapabilities:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        plan = state.plan
        assert plan is not None, "check_capabilities reached with no plan"

        if plan.needs_clarification:
            return {"missing": [{"type": "clarification", "question": plan.needs_clarification}]}

        missing = []
        for step in plan.steps:
            for capability in step.required_capabilities:
                if capability not in state.available_capabilities:
                    missing.append(
                        {
                            "type": "missing_capability",
                            "capability": capability,
                            "needed_for": step.goal,
                            "options": [
                                {
                                    "kind": "info",
                                    "label": "No connectors are installed in this workspace yet",
                                }
                            ],
                        }
                    )
        return {"missing": missing}
