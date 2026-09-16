"""`synthesize` (docs/system-design.md section 8.5): writes the final answer from what each
step produced, streaming tokens the same way `direct_answer` does. Citations and artifact
links (the rest of section 8.5's description) wait for the documents connector and sandbox
(Phase 4) — Phase 3 steps only ever produce a plain-text `result_summary`, nothing to cite or
attach yet. `validate_final`'s groundedness check is a Phase 7 addition; until then this is the
graph's last stop before `finalize`.
"""

from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState
from relay_core.events.types import TOKEN
from relay_core.llm.profiles import EXECUTOR

_SYSTEM_PROMPT = """\
You are writing the final answer for Relay, an operations agent, after running a multi-step
plan. Use ONLY the step results below — never invent data that isn't there. If a step failed
or was skipped, say so plainly and explain what that means for the answer rather than glossing
over it. Be concrete: use the actual numbers/names from the step results, not vague summaries.
"""


class Synthesize:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        plan = state.plan
        assert plan is not None, "synthesize reached with no plan"

        lines = [f"Objective: {plan.objective}", ""]
        lines.extend(
            f"[{step.status}] {step.goal}: {step.result_summary or '(no result)'}"
            for step in plan.steps
        )
        contents = [{"role": "user", "parts": [{"text": "\n".join(lines)}]}]

        async def on_delta(delta: str) -> None:
            await self.deps.events.publish(state.run_id, TOKEN, {"delta": delta})

        resp = await self.deps.gateway.generate_stream(
            role=EXECUTOR,
            system=_SYSTEM_PROMPT,
            contents=contents,
            workspace_id=state.workspace_id,
            run_id=state.run_id,
            on_delta=on_delta,
            settings=self.deps.settings,
        )
        return {
            "final_answer": resp.text or "I finished the plan but couldn't summarize the results."
        }
