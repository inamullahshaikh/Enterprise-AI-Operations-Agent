"""`direct_answer` (docs/system-design.md sections 8.5, 8.9): answers messages
that need no plan or tools at all, streaming tokens as they arrive so the chat
UI can render them incrementally.
"""

from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState
from relay_core.events.types import TOKEN
from relay_core.llm.profiles import EXECUTOR

_SYSTEM_PROMPT = """\
You are Relay, an operations agent inside a company workspace. Answer the user's
message directly and concisely — this path is only used for messages that need no
company data lookup and no multi-step action. If the request turns out to need
either of those, say so rather than guessing.
"""


class DirectAnswer:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        contents = _build_contents(state)

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
        return {"final_answer": resp.text or ""}


def _build_contents(state: AgentState) -> list[dict[str, Any]]:
    turns = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in state.recent_messages
        if m["role"] in ("user", "assistant")
    ]
    turns.append({"role": "user", "parts": [{"text": state.user_message}]})
    return turns
