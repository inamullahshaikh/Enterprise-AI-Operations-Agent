"""`route` (docs/system-design.md sections 8.5, 8.9): decides between the
`direct_answer` fast path and the plan-and-execute path. Conservative by
design — an uncertain classification should still get a plan (and, in Phase 2,
an honest missing-capability card) rather than a direct answer that might
silently skip work the user actually needed done.
"""

from typing import Any

from pydantic import BaseModel

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState
from relay_core.llm.profiles import LIGHT
from relay_core.llm.schemas import parse_structured

_SYSTEM_PROMPT = """\
Classify the user's message for Relay, an operations agent inside a company workspace.

Return "direct" only if the message needs no external data and no multi-step action —
chit-chat, general knowledge, or rephrasing/explaining something already in the
conversation. Return "task" for anything that needs looking something up, analyzing
company data, or taking an action, and whenever you are not sure which it is.

Return JSON matching the schema.
"""


class RouteVerdict(BaseModel):
    route: str  # "direct" | "task"


class Route:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        resp = await self.deps.gateway.generate(
            role=LIGHT,
            system=_SYSTEM_PROMPT,
            contents=state.user_message,
            workspace_id=state.workspace_id,
            run_id=state.run_id,
            response_schema=RouteVerdict,
            settings=self.deps.settings,
        )
        verdict = parse_structured(resp, RouteVerdict)
        route = verdict.route if verdict and verdict.route == "direct" else "task"
        return {"route": route}
