"""`finalize` (docs/system-design.md section 8.5): the terminal node for the `direct_answer`,
`blocked` (guard refusal), and `synthesize` paths — persists the final answer, rolls up usage
(including `tool_calls`, now that Phase 3's executor produces some), and marks the run
`completed`. Every path reaching this node already sets `final_answer` itself; the fallback
below is defensive, not a real gap to call out (unlike Phase 2, where it covered step execution
not existing yet at all).
"""

from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState
from relay_core.events.types import RUN_COMPLETED


class Finalize:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        content = state.final_answer or "I wasn't able to produce an answer for that."

        message = await self.deps.messages.create(
            workspace_id=state.workspace_id,
            conversation_id=state.conversation_id,
            role="assistant",
            content=content,
        )
        usage = await self.deps.llm_calls.sum_usage_for_run(state.workspace_id, state.run_id)
        tool_call_count = await self.deps.tool_calls.count_for_run(state.workspace_id, state.run_id)
        await self.deps.runs.mark_completed(
            state.workspace_id,
            state.run_id,
            final_message_id=message.id,
            route=state.route,
            usage=usage,
            tool_calls=tool_call_count,
            capability_snapshot=state.available_capabilities or None,
        )
        await self.deps.conversations.touch(state.workspace_id, state.conversation_id)
        await self.deps.events.publish(
            state.run_id, RUN_COMPLETED, {"message_id": str(message.id), "status": "completed"}
        )
        return {}
