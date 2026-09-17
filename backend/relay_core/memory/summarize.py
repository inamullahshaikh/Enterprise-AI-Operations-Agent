"""Rolling conversation summaries (docs/system-design.md section 12.1).

A long conversation can't send every message on every turn, so everything older than the last
`KEEP_RECENT` messages is folded into `conversations.summary` and `load_context` sends that
instead. `summary_upto_message_id` is the watermark: the next summary starts from the message
after it, so the same history is never re-read or re-billed.

Section 12.1's trigger is "~20 messages or ~30k tokens". Only the message count is checked here.
Counting tokens properly means a tokenizer call per message, and the message count is what
actually correlates with a long conversation — a 30k-token *pair* of messages is a pasted
document, which belongs in the knowledge base, not in a summary.

Re-summarizing folds the previous summary into the new one rather than starting over, so a fact
from message 3 can still be there at message 300 without message 3 ever being read again.
"""

import uuid

from relay_core.config import Settings
from relay_core.db.models.conversations import Message
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.profiles import LIGHT

# The last N messages are always sent verbatim (section 12.1), so they are never summarized.
KEEP_RECENT = 8
# How many messages may pile up past the watermark before a new summary is worth its LLM call.
SUMMARIZE_AFTER = 20
_MAX_MESSAGES_PER_PASS = 200

_SYSTEM_PROMPT = """\
You maintain the running summary of one conversation between a user and Relay, an operations
agent inside a company workspace.

Write the summary that should be carried forward. Keep what a later turn would need: what the
user is trying to do, decisions and preferences they stated, facts established, and anything
left unfinished. Drop pleasantries, restated questions, and detail that has since been
superseded.

Write it as plain prose in at most 200 words, in the third person ("The user asked ..."). Do not
add anything the messages below do not say.
"""


async def summarize_conversation(
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    conversations: ConversationRepository,
    messages: MessageRepository,
    gateway: LLMGateway,
    settings: Settings,
    run_id: uuid.UUID | None = None,
) -> str | None:
    """Summarizes everything past the current watermark except the last `KEEP_RECENT` messages,
    and moves the watermark. Returns the new summary, or `None` when the conversation is too
    short to need one. The caller commits.

    Takes repositories rather than a session because its one in-graph caller (`finalize`) has
    repositories and no session — every node in `relay_core.agent.nodes` is built that way.
    """
    conversation = await conversations.get(workspace_id, conversation_id)
    if conversation is None:
        return None

    history = await messages.list_for_conversation(
        workspace_id, conversation_id, limit=_MAX_MESSAGES_PER_PASS
    )
    pending = _after_watermark(history, conversation.summary_upto_message_id)
    to_summarize = pending[:-KEEP_RECENT]
    if len(pending) <= SUMMARIZE_AFTER or not to_summarize:
        return None

    previous = (
        f"Summary of the conversation so far:\n{conversation.summary}\n\n"
        if conversation.summary
        else ""
    )
    transcript = "\n".join(f"{m.role}: {m.content}" for m in to_summarize)

    resp = await gateway.generate(
        role=LIGHT,
        system=_SYSTEM_PROMPT,
        contents=f"{previous}Messages since then:\n{transcript}",
        workspace_id=workspace_id,
        run_id=run_id,
        settings=settings,
    )
    summary = (resp.text or "").strip()
    if not summary:
        return None

    await conversations.set_summary(
        workspace_id, conversation_id, summary=summary, upto_message_id=to_summarize[-1].id
    )
    return summary


def _after_watermark(history: list[Message], watermark: uuid.UUID | None) -> list[Message]:
    """`list_for_conversation` returns oldest-first, so everything after the watermark is the
    tail past it. A watermark that isn't in this window means the window is already entirely
    newer than it (the pass cap), so all of it is pending."""
    if watermark is None:
        return history
    ids = [m.id for m in history]
    return history[ids.index(watermark) + 1 :] if watermark in ids else history
