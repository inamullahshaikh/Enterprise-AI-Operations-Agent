"""Conversation & message routes (docs/system-design.md section 15.4).

Conversations are private to the user who started them — even other members of
the same workspace get a 404 (not 403) on someone else's conversation, matching
the existing "don't confirm existence to a non-owner" convention in
`relay_api/deps.py::require_workspace_role`.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, UploadFile, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import (
    CurrentUser,
    RunDispatcher,
    get_object_store,
    get_redis,
    get_run_dispatcher,
    require_workspace_role,
)
from relay_api.errors import ProblemDetail
from relay_api.ratelimit import message_rate_limit
from relay_core.connectors.builtin.csv_profile import infer_capabilities, profile_csv
from relay_core.db.models.attachments import Attachment
from relay_core.db.models.conversations import Conversation, Message
from relay_core.db.repositories.agent_runs import AgentRunRepository
from relay_core.db.repositories.attachments import AttachmentRepository
from relay_core.db.repositories.conversations import ConversationRepository
from relay_core.db.repositories.messages import MessageRepository
from relay_core.db.repositories.workspaces import WorkspaceRepository
from relay_core.db.session import get_session
from relay_core.policy.budgets import budget_resets_at, month_spend
from relay_core.security.rbac import Role
from relay_core.storage.object_store import ObjectStore

router = APIRouter(prefix="/workspaces/{workspace_id}/conversations", tags=["conversations"])

_TITLE_PREVIEW_LEN = 60


class CreateConversationRequest(BaseModel):
    title: str | None = None


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str | None
    is_archived: bool
    last_message_at: str | None = None

    @classmethod
    def from_model(cls, c: Conversation) -> "ConversationOut":
        return cls(
            id=c.id,
            title=c.title,
            is_archived=c.is_archived,
            last_message_at=c.last_message_at.isoformat() if c.last_message_at else None,
        )


class UpdateConversationRequest(BaseModel):
    title: str | None = None
    is_archived: bool | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    content_json: dict[str, Any] | None
    created_at: str

    @classmethod
    def from_model(cls, m: Message) -> "MessageOut":
        return cls(
            id=m.id,
            role=m.role,
            content=m.content,
            content_json=m.content_json,
            created_at=m.created_at.isoformat(),
        )


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1)


class SendMessageResponse(BaseModel):
    message_id: uuid.UUID
    run_id: uuid.UUID
    events_url: str


async def _owned_conversation(
    session: AsyncSession, workspace_id: uuid.UUID, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Conversation:
    conversation = await ConversationRepository(session).get(workspace_id, conversation_id)
    if conversation is None or conversation.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationRequest,
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    conversation = await ConversationRepository(session).create(
        workspace_id=workspace_id, user_id=current.user.id, title=body.title
    )
    return ConversationOut.from_model(conversation)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationOut]:
    conversations = await ConversationRepository(session).list_for_user(
        workspace_id, current.user.id
    )
    return [ConversationOut.from_model(c) for c in conversations]


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    workspace_id: uuid.UUID = Path(...),
    conversation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    conversation = await _owned_conversation(
        session, workspace_id, conversation_id, current.user.id
    )
    return ConversationOut.from_model(conversation)


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    body: UpdateConversationRequest,
    workspace_id: uuid.UUID = Path(...),
    conversation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    conversation = await _owned_conversation(
        session, workspace_id, conversation_id, current.user.id
    )
    if body.title is not None:
        conversation.title = body.title
    if body.is_archived is not None:
        conversation.is_archived = body.is_archived
    return ConversationOut.from_model(conversation)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    workspace_id: uuid.UUID = Path(...),
    conversation_id: uuid.UUID = Path(...),
    before: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> list[MessageOut]:
    await _owned_conversation(session, workspace_id, conversation_id, current.user.id)
    messages = await MessageRepository(session).list_for_conversation(
        workspace_id, conversation_id, limit=limit, before=before
    )
    return [MessageOut.from_model(m) for m in messages]


@router.post(
    "/{conversation_id}/messages",
    response_model=SendMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(message_rate_limit())],
)
async def send_message(
    body: SendMessageRequest,
    workspace_id: uuid.UUID = Path(...),
    conversation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.member.name)),
    session: AsyncSession = Depends(get_session),
    dispatch: RunDispatcher = Depends(get_run_dispatcher),
    redis: Redis = Depends(get_redis),
) -> SendMessageResponse:
    await _owned_conversation(session, workspace_id, conversation_id, current.user.id)

    # Section 19.3's conversation lock, read from the runs table rather than a Redis key: the
    # row's status already says whether a run is active, and it cannot outlive its run. A dead
    # worker's `running` row is failed by the watchdog (`fail_stalled_runs`), which frees it.
    # ponytail: two messages racing in the same instant can both pass; a partial unique index on
    # (conversation_id) WHERE status is active closes that if it is ever seen.
    active = await AgentRunRepository(session).active_for_conversation(
        workspace_id, conversation_id
    )
    if active is not None:
        raise ProblemDetail(
            status.HTTP_409_CONFLICT,
            "A run is already active in this conversation",
            detail=f"Run {active.id} is {active.status}; wait for it to finish.",
            type_="https://relay.dev/problems/conversation-busy",
        )

    # Section 19.2: refused before a run row exists. A run already queued or running when the
    # cap is reached is left to finish.
    workspace = await WorkspaceRepository(session).get(workspace_id)
    assert workspace is not None
    spent = await month_spend(session, redis, workspace_id)
    if spent >= workspace.monthly_budget_usd:
        raise ProblemDetail(
            status.HTTP_402_PAYMENT_REQUIRED,
            "Monthly budget exhausted",
            detail=(
                f"This workspace has spent ${spent:.2f} of its ${workspace.monthly_budget_usd:.2f}"
                f" monthly budget. It resets on {budget_resets_at():%Y-%m-%d}."
            ),
            type_="https://relay.dev/problems/monthly-budget-exceeded",
        )

    messages = MessageRepository(session)
    message = await messages.create(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        role="user",
        content=body.content,
    )
    run = await AgentRunRepository(session).create(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        user_id=current.user.id,
        trigger_message_id=message.id,
    )
    await ConversationRepository(session).touch(
        workspace_id, conversation_id, title=body.content[:_TITLE_PREVIEW_LEN]
    )
    # Commit before dispatching, not after returning: the worker runs in a
    # separate process against a separate connection, and a Celery task can be
    # dequeued and start querying for this row within milliseconds — well
    # before `get_session`'s own post-handler commit would otherwise run. A
    # `flush()` alone only makes the row visible within *this* transaction.
    await session.commit()
    await dispatch(workspace_id, run.id)

    return SendMessageResponse(
        message_id=message.id,
        run_id=run.id,
        events_url=f"/api/v1/workspaces/{workspace_id}/runs/{run.id}/events",
    )


class AttachmentOut(BaseModel):
    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    row_count: int | None
    columns: list[str] | None
    inferred_capabilities: list[str]

    @classmethod
    def from_model(cls, a: Attachment) -> "AttachmentOut":
        return cls(
            id=a.id,
            filename=a.filename,
            mime_type=a.mime_type,
            size_bytes=a.size_bytes,
            row_count=(a.profile or {}).get("row_count"),
            columns=(a.profile or {}).get("columns"),
            inferred_capabilities=a.inferred_capabilities,
        )


@router.get("/{conversation_id}/attachments", response_model=list[AttachmentOut])
async def list_attachments(
    workspace_id: uuid.UUID = Path(...),
    conversation_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.viewer.name)),
    session: AsyncSession = Depends(get_session),
) -> list[AttachmentOut]:
    await _owned_conversation(session, workspace_id, conversation_id, current.user.id)
    attachments = await AttachmentRepository(session).list_for_conversation(
        workspace_id, conversation_id
    )
    return [AttachmentOut.from_model(a) for a in attachments]


@router.post(
    "/{conversation_id}/attachments",
    response_model=AttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    workspace_id: uuid.UUID = Path(...),
    conversation_id: uuid.UUID = Path(...),
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_workspace_role(Role.member.name)),
    session: AsyncSession = Depends(get_session),
    object_store: ObjectStore = Depends(get_object_store),
) -> AttachmentOut:
    """CSV only for now (docs/adr/0009) — XLSX/PDF/DOCX wait for the Phase 4 ingestion
    pipeline, which needs async processing that a synchronous profile-on-upload doesn't.
    No explicit size cap yet: a hard per-upload byte limit is a Phase 8 hardening concern
    (section 19), alongside the rate limits and budgets that belong with it.
    """
    await _owned_conversation(session, workspace_id, conversation_id, current.user.id)

    filename = file.filename or "upload.csv"
    if not filename.lower().endswith(".csv"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only .csv files are supported right now (XLSX/PDF/DOCX land in later phases)",
        )

    raw = await file.read()
    profile = profile_csv(raw)
    inferred = infer_capabilities(profile.columns)

    blob_key = f"attachments/{workspace_id}/{uuid.uuid4()}/{filename}"
    await object_store.put_bytes(blob_key, raw, content_type=file.content_type or "text/csv")

    attachment = await AttachmentRepository(session).create(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        filename=filename,
        mime_type=file.content_type or "text/csv",
        size_bytes=len(raw),
        blob_key=blob_key,
        kind="table",
        profile=profile.to_json(),
        inferred_capabilities=inferred,
        uploaded_by=current.user.id,
    )
    return AttachmentOut.from_model(attachment)
