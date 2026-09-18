"""`finalize` (docs/system-design.md section 8.5): the terminal node for the `direct_answer`,
`blocked` (guard refusal), and `synthesize` paths — persists the final answer, rolls up usage
(including `tool_calls`, now that Phase 3's executor produces some), and marks the run
`completed`. Every path reaching this node already sets `final_answer` itself; the fallback
below is defensive, not a real gap to call out (unlike Phase 2, where it covered step execution
not existing yet at all).

Section 8.5 also gives this node the two jobs that keep the *next* turn informed (section 12):
extracting durable memories from the run, and re-summarizing a conversation that has grown past
what `load_context` sends verbatim. Both run after the answer is persisted and published, and
neither can fail the run — the user already has what they asked for, and a missing memory is not
worth turning a completed run into a failed one.

Extraction is enqueued (`deps.extract_memories`, which waits for this transaction to commit
before the task is queued); summarization is awaited inline. The asymmetry is deliberate:
extraction runs after every completed run and costs a call each time, while summarization fires
on roughly one turn in twenty, so a queue hop would be more machinery than the work it defers.
"""

import logging
from typing import Any

from relay_core.agent.deps import AgentDeps
from relay_core.agent.state import AgentState
from relay_core.events.types import RUN_COMPLETED
from relay_core.memory.summarize import summarize_conversation
from relay_core.policy.budgets import invalidate_month_spend
from relay_core.security.pii import restore

logger = logging.getLogger(__name__)


class Finalize:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    async def __call__(self, state: AgentState) -> dict[str, Any]:
        content = restore(
            state.final_answer or "I wasn't able to produce an answer for that.", state.pii_map
        )

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
        await invalidate_month_spend(self.deps.events.redis, state.workspace_id)
        await self.deps.events.publish(
            state.run_id, RUN_COMPLETED, {"message_id": str(message.id), "status": "completed"}
        )

        if state.route != "blocked":
            await self._remember(state)
        return {}

    async def _remember(self, state: AgentState) -> None:
        """Memory is a workspace setting (section 12, ADR-0013 decision 6), so both halves check
        it — extraction checks again inside the task, because the policy can change between the
        enqueue and the worker picking it up."""
        policy = await self.deps.policies.get(state.workspace_id)
        if not policy.memory_enabled:
            return
        try:
            await self.deps.extract_memories(state.workspace_id, state.run_id)
            await summarize_conversation(
                workspace_id=state.workspace_id,
                conversation_id=state.conversation_id,
                conversations=self.deps.conversations,
                messages=self.deps.messages,
                gateway=self.deps.gateway,
                settings=self.deps.settings,
                run_id=state.run_id,
            )
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.exception("post-run memory work failed for run %s", state.run_id)
