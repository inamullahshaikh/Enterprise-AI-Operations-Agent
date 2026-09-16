"""`ask_missing` (docs/system-design.md sections 7.3, 8.5): the graph's one
"pause for the user" outcome in Phase 2. Per the state diagram (section 8.2,
`ask_missing --> [*]`) this ends the run directly rather than routing through
`finalize` — so it does `finalize`'s bookkeeping itself (persist the message,
roll up usage, mark the run terminal, publish the closing event).

Resolving the gap (connect a connector, upload a file, skip and replan) is
Phase 3/6/7 — the card here is informational only.
"""

from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState
from relay_core.events.types import CAPABILITIES_MISSING, QUESTION_ASKED, RUN_COMPLETED


class AskMissing:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        clarification = next((m for m in state.missing if m["type"] == "clarification"), None)

        if clarification is not None:
            content = clarification["question"]
            content_json = {"type": "clarification_needed", "question": clarification["question"]}
            await self.deps.events.publish(state.run_id, QUESTION_ASKED, content_json)
        else:
            content = _summarize(state.missing)
            content_json = {
                "type": "missing_capabilities",
                "missing": state.missing,
                "can_partially_complete": False,
            }
            await self.deps.events.publish(state.run_id, CAPABILITIES_MISSING, content_json)

        message = await self.deps.messages.create(
            workspace_id=state.workspace_id,
            conversation_id=state.conversation_id,
            role="assistant",
            content=content,
            content_json=content_json,
        )
        usage = await self.deps.llm_calls.sum_usage_for_run(state.workspace_id, state.run_id)
        await self.deps.runs.mark_awaiting_input(
            state.workspace_id,
            state.run_id,
            final_message_id=message.id,
            route=state.route,
            missing_capabilities=content_json,
            usage=usage,
            capability_snapshot=state.available_capabilities or None,
        )
        await self.deps.conversations.touch(state.workspace_id, state.conversation_id)
        await self.deps.events.publish(
            state.run_id, RUN_COMPLETED, {"message_id": str(message.id), "status": "awaiting_input"}
        )
        return {}


def _summarize(missing: list[dict[str, Any]]) -> str:
    capabilities = [m["capability"] for m in missing if m["type"] == "missing_capability"]
    if not capabilities:
        return "I need more information before I can continue with this."
    unique = ", ".join(dict.fromkeys(capabilities))
    return (
        "I can't finish this yet — it needs "
        f"{unique}, and no connector provides that in this workspace. "
        "Once one is connected, ask me again and I'll pick up where I left off."
    )
