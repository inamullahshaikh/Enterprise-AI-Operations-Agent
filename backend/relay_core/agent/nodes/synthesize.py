"""`synthesize` (docs/system-design.md section 8.5): writes the final answer from what each
step produced.

Unlike `direct_answer`, it does not publish its tokens as they arrive. `validate_final` runs
after it and can send the draft back for one revision, and an answer the user watched appear and
then silently change is worse than an answer that arrives a second later (ADR-0013 decision 5).
So the deltas are collected into `draft_chunks` and `validate_final` publishes them once the
draft is the one that will be finalized.

A revision pass gets the validator's `unsupported_claims` and is told to cut or qualify them.
It is never told to find support: inventing a citation for a number that was never produced is
the failure this check exists to catch.
"""

from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState
from relay_core.llm.profiles import EXECUTOR

_SYSTEM_PROMPT = """\
You are writing the final answer for Relay, an operations agent, after running a multi-step
plan. Use ONLY the step results below — never invent data that isn't there. If a step failed
or was skipped, say so plainly and explain what that means for the answer rather than glossing
over it. Be concrete: use the actual numbers/names from the step results, not vague summaries.
"""

_REVISION_PROMPT = """\

Your previous draft made claims the step results do not support:
{claims}

Write the answer again without them. Cut each unsupported claim, or qualify it as something
that was not checked. Never invent a source or a number to support one.
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
        system = _SYSTEM_PROMPT
        if state.unsupported_claims:
            system += _REVISION_PROMPT.format(
                claims="\n".join(f"- {claim}" for claim in state.unsupported_claims)
            )

        chunks: list[str] = []

        async def on_delta(delta: str) -> None:
            chunks.append(delta)

        resp = await self.deps.gateway.generate_stream(
            role=EXECUTOR,
            system=system,
            contents=contents,
            workspace_id=state.workspace_id,
            run_id=state.run_id,
            on_delta=on_delta,
            settings=self.deps.settings,
        )
        answer = resp.text or "I finished the plan but couldn't summarize the results."
        return {"final_answer": answer, "draft_chunks": chunks or [answer]}
