"""`validate_final` (docs/system-design.md sections 8.2, 8.5): the last thing between a draft
answer and the user. It checks that every claim in the draft traces back to a step result or a
cited source, and sends the draft back to `synthesize` once if it doesn't.

**This node is why `synthesize` buffers instead of streaming** (ADR-0013 decision 5). A
groundedness check that can demand a revision is worthless if the unvalidated text is already on
the user's screen, so `synthesize` collects its deltas into `draft_chunks` and this node
publishes them — in the same pieces they arrived in — only once the draft is the one that will
be finalized. Nothing from a rejected draft is ever published.

**It degrades to a no-op rather than blocking an answer.** An unparseable verdict, or a second
`revise` after the one allowed revision, finalizes the draft anyway. The check exists to catch a
hallucinated number, not to become a new way for a run to end with nothing.

`direct_answer` and `blocked` never reach here: there are no tool outputs for them to be
grounded in, and they stream to the user directly as they always have.
"""

from typing import Any, Literal

from pydantic import BaseModel

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState
from relay_core.events.types import TOKEN
from relay_core.llm.profiles import VALIDATOR
from relay_core.llm.schemas import parse_structured
from relay_core.security.pii import restore

# Section 8.2's "revise (max 1)".
MAX_FINAL_REVISIONS = 1

_SYSTEM_PROMPT = """\
You are checking a final answer for Relay, an operations agent, before it reaches the user.

What the run actually produced:
{evidence}

The draft answer:
{draft}

Return "revise" if the draft states anything the evidence above does not support — a number
that appears nowhere, a name that was never returned, a claim about something no step checked.
Saying that a step failed or that data is missing is supported, not unsupported: honesty about a
gap is what we want. Everything else is "pass".

List each unsupported claim as the shortest quote from the draft that contains it.
`reason` should be one sentence a user could read.
Return JSON matching the schema.
"""


class FinalVerdict(BaseModel):
    status: Literal["pass", "revise"]
    unsupported_claims: list[str] = []
    reason: str


class ValidateFinal:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        draft = state.final_answer or ""
        if state.final_revisions >= MAX_FINAL_REVISIONS or not draft:
            # The one revision has been spent, so this draft ships whatever the verdict would
            # be. Skipping the call as well as the revision keeps the cost at one validator
            # call per pass rather than one per pass plus a wasted one at the end.
            return await self._publish(state)

        plan = state.plan
        evidence = [f"Objective: {plan.objective}", ""] if plan is not None else []
        evidence.extend(
            f"[{step.status}] {step.goal}: {step.result_summary or '(no result)'}"
            for step in (plan.steps if plan is not None else [])
        )
        evidence.extend(f"Source: {source}" for source in state.sources)

        resp = await self.deps.gateway.generate(
            role=VALIDATOR,
            system=_SYSTEM_PROMPT.format(evidence="\n".join(evidence), draft=draft),
            contents="Check this answer against the evidence.",
            workspace_id=state.workspace_id,
            run_id=state.run_id,
            response_schema=FinalVerdict,
            settings=self.deps.settings,
        )
        verdict = parse_structured(resp, FinalVerdict)

        if verdict is None or verdict.status == "pass":
            return await self._publish(state)
        return {
            "final_revisions": state.final_revisions + 1,
            "unsupported_claims": verdict.unsupported_claims,
            # The rejected draft is dropped here, not in `synthesize`: nothing downstream should
            # be able to publish or persist text that failed the check.
            "final_answer": None,
            "draft_chunks": [],
        }

    async def _publish(self, state: AgentState) -> dict[str, Any]:
        chunks = state.draft_chunks
        if state.pii_map:
            # A placeholder can straddle two chunks, so restore the whole draft at once.
            chunks = [restore("".join(chunks), state.pii_map)]
        for chunk in chunks:
            await self.deps.events.publish(state.run_id, TOKEN, {"delta": chunk})
        return {"draft_chunks": [], "unsupported_claims": []}
