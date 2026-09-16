"""`load_context` (docs/system-design.md section 8.5): loads what every later node needs from
the conversation. Conversation summaries (section 12.1) and memories (section 12) are Phase 7
features — `history_summary` is always `None` and there's no `memories` field on `AgentState`
yet. `available_capabilities` is now resolved for real (`relay_core.capabilities.resolver`,
Phase 3) instead of hardcoded to `[]`.
"""

from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState
from relay_core.capabilities.resolver import resolve_available_capabilities

_HISTORY_LIMIT = 8


class LoadContext:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        history = await self.deps.messages.list_for_conversation(
            state.workspace_id,
            state.conversation_id,
            limit=_HISTORY_LIMIT,
            before=state.trigger_message_id,
        )
        available_capabilities = await resolve_available_capabilities(
            self.deps.connector_installations,
            self.deps.attachments,
            self.deps.documents,
            workspace_id=state.workspace_id,
            conversation_id=state.conversation_id,
        )
        return {
            "recent_messages": [{"role": m.role, "content": m.content} for m in history],
            "available_capabilities": available_capabilities,
        }
