"""Conversation summarization (Phase 7 D4, docs/system-design.md section 12.1).

The watermark is what these tests are really about. A summary without a correct
`summary_upto_message_id` looks right in the database and is wrong on the next turn: the same
messages get summarized again, and `load_context` sends a summary that overlaps the verbatim
tail it is supposed to replace.
"""

import uuid

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.config import Settings
from relay_core.db.models.conversations import Conversation
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.db.repositories.users import UserRepository
from relay_core.db.repositories.workspaces import WorkspaceRepository
from relay_core.memory.summarize import KEEP_RECENT, summarize_conversation
from tests.integration.scripted_model import scripted_gateway, text_response

pytestmark = pytest.mark.asyncio

_SUMMARY = "The user asked about renewals and prefers formal drafts."
_LATER_SUMMARY = "The user then moved on to scheduling."


@pytest_asyncio.fixture
async def redis_client(redis_url: str):
    client = Redis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


async def _conversation(session: AsyncSession, message_count: int) -> Conversation:
    user = await UserRepository(session).create(
        email=f"sum-{uuid.uuid4().hex[:8]}@example.com", full_name="Dev", password_hash="x"
    )
    await session.flush()
    workspace = await WorkspaceRepository(session).create(
        name="Summary Co", slug=f"sum-{uuid.uuid4().hex[:8]}", created_by=user.id
    )
    await session.flush()
    conversation = await ConversationRepository(session).create(
        workspace_id=workspace.id, user_id=user.id
    )
    await session.flush()

    messages = MessageRepository(session)
    for i in range(message_count):
        await messages.create(
            workspace_id=workspace.id,
            conversation_id=conversation.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"message {i}",
        )
    await session.flush()
    return conversation


async def _summarize(
    conversation: Conversation,
    db_session: AsyncSession,
    redis_client,
    test_settings: Settings,
    *texts: str,
):
    gateway, models = scripted_gateway(
        db_session,
        redis_client,
        test_settings,
        responses=[text_response(t) for t in texts],
        stream_texts=[],
    )
    summary = await summarize_conversation(
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        conversations=ConversationRepository(db_session),
        messages=MessageRepository(db_session),
        gateway=gateway,
        settings=test_settings,
    )
    return summary, models


async def test_a_long_conversation_is_summarized_up_to_the_last_eight_messages(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    conversation = await _conversation(db_session, 25)

    summary, models = await _summarize(
        conversation, db_session, redis_client, test_settings, _SUMMARY
    )

    assert summary == _SUMMARY
    stored = await ConversationRepository(db_session).get(
        conversation.workspace_id, conversation.id
    )
    assert stored is not None
    assert stored.summary == _SUMMARY

    history = await MessageRepository(db_session).list_for_conversation(
        conversation.workspace_id, conversation.id, limit=100
    )
    # 25 messages, the last 8 kept verbatim: the watermark sits on message 16 (index 16 of 25).
    assert stored.summary_upto_message_id == history[-KEEP_RECENT - 1].id
    assert f"message {len(history) - KEEP_RECENT - 1}" in str(models.contents[0])
    assert f"message {len(history) - 1}" not in str(models.contents[0])


async def test_a_short_conversation_gets_no_summary_and_no_model_call(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    conversation = await _conversation(db_session, 12)

    summary, models = await _summarize(
        conversation, db_session, redis_client, test_settings, _SUMMARY
    )

    assert summary is None
    assert models.calls == []
    stored = await ConversationRepository(db_session).get(
        conversation.workspace_id, conversation.id
    )
    assert stored is not None and stored.summary is None


async def test_re_summarizing_folds_the_previous_summary_in_and_moves_the_watermark(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    conversation = await _conversation(db_session, 25)
    _, _ = await _summarize(conversation, db_session, redis_client, test_settings, _SUMMARY)
    first_watermark = (
        await ConversationRepository(db_session).get(conversation.workspace_id, conversation.id)
    ).summary_upto_message_id

    # Another 25 messages arrive.
    messages = MessageRepository(db_session)
    for i in range(25, 50):
        await messages.create(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"message {i}",
        )
    await db_session.flush()

    summary, models = await _summarize(
        conversation, db_session, redis_client, test_settings, _LATER_SUMMARY
    )

    assert summary == _LATER_SUMMARY
    sent = str(models.contents[0])
    assert _SUMMARY in sent, "the previous summary is folded in, not thrown away"
    assert "message 5" not in sent, "messages already behind the watermark are not re-read"
    stored = await ConversationRepository(db_session).get(
        conversation.workspace_id, conversation.id
    )
    assert stored is not None and stored.summary_upto_message_id != first_watermark


async def test_the_next_runs_context_carries_the_summary_plus_the_last_eight_messages(
    db_session: AsyncSession, redis_client, test_settings: Settings
) -> None:
    """`load_context` is what turns a stored summary into something a prompt sees: the summary
    column plus `_HISTORY_LIMIT` verbatim messages, which is exactly section 12.1's contract."""
    conversation = await _conversation(db_session, 25)
    await _summarize(conversation, db_session, redis_client, test_settings, _SUMMARY)

    stored = await ConversationRepository(db_session).get(
        conversation.workspace_id, conversation.id
    )
    history = await MessageRepository(db_session).list_for_conversation(
        conversation.workspace_id, conversation.id, limit=KEEP_RECENT
    )

    assert stored is not None and stored.summary == _SUMMARY
    assert [m.content for m in history] == [f"message {i}" for i in range(25 - KEEP_RECENT, 25)]
