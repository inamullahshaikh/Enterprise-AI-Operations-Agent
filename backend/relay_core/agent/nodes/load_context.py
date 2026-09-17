"""`load_context` (docs/system-design.md section 8.5): loads what every later node needs from
the conversation. Phase 7 fills in the two fields that were placeholders until now:
`history_summary` comes from `conversations.summary` (section 12.1, written by
`relay_core.memory.summarize`), and `memories` is the top handful of remembered facts and
preferences for this user and workspace (section 12.3).

Memory retrieval is best-effort. An embedding call that fails leaves `memories` empty and the
run carries on — the same posture as Phase 6's tool retrieval, and for the same reason: a
degraded answer beats no answer, and nothing here is load-bearing for correctness.

`available_capabilities` is resolved for real (`relay_core.capabilities.resolver`, Phase 3), and
Phase 5 adds `user_role`, which section 13.1's approval rules need.
"""

import logging
from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState
from relay_core.capabilities.resolver import resolve_available_capabilities

logger = logging.getLogger(__name__)

_HISTORY_LIMIT = 8
_MEMORY_LIMIT = 5


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
            self.deps.tool_definitions,
            self.deps.attachments,
            self.deps.documents,
            workspace_id=state.workspace_id,
            conversation_id=state.conversation_id,
        )
        # Membership can be revoked between the message being queued and the worker picking it
        # up, so fall back to the least privileged role — that direction only ever asks for
        # more approvals, never fewer.
        member = await self.deps.members.get(state.workspace_id, state.user_id)
        conversation = await self.deps.conversations.get(
            state.workspace_id, state.conversation_id
        )
        return {
            "recent_messages": [{"role": m.role, "content": m.content} for m in history],
            "history_summary": conversation.summary if conversation is not None else None,
            "memories": await self._memories(state),
            "available_capabilities": available_capabilities,
            "user_role": member.role if member is not None else "viewer",
        }

    async def _memories(self, state: AgentState) -> list[str]:
        policy = await self.deps.policies.get(state.workspace_id)
        if not policy.memory_enabled:
            return []
        try:
            [vector] = await self.deps.gateway.embed(
                [state.user_message], task="RETRIEVAL_QUERY", settings=self.deps.settings
            )
        except Exception:  # noqa: BLE001 - see the module docstring: memory is best-effort
            logger.warning("memory retrieval skipped for run %s", state.run_id, exc_info=True)
            return []

        found = await self.deps.memories.nearest(
            state.workspace_id, state.user_id, vector, limit=_MEMORY_LIMIT
        )
        # Marked used here rather than at the end of the run: what a run *was told* is what the
        # counter is for, and a run that ends early still consumed the memory it was given.
        await self.deps.memories.mark_used(state.workspace_id, [m.id for m in found])
        return [m.content for m in found]
