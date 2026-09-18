"""Memory management routes (docs/system-design.md sections 12.2, 15.5): what the agent
remembered, and the ability to correct it.

A memory changes how the agent behaves on every later run, so "view, edit and delete" is not a
convenience — it is the only way a user can undo a wrong conclusion the extractor drew about
them. That is why `DELETE` really deletes rather than deactivating: a user who asks for
something to be forgotten means forgotten.

Permissions split on scope, not on route. A member owns their own memories outright; a
workspace-scope memory is everyone's, so only an owner or admin may change or remove one. A
non-member gets 404 from `require_workspace_role`, like every other router here.

Embeddings never leave this process: they are large, meaningless to a client, and re-derived
from `content` whenever it changes.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from relay_api.deps import CurrentUser, get_llm_gateway, get_settings_dep, require_workspace_role
from relay_core.config import Settings
from relay_core.db.models.memories import KINDS, Memory
from relay_core.db.repositories.audit import AuditLogRepository
from relay_core.db.repositories.memories import MemoryRepository
from relay_core.db.session import get_session
from relay_core.llm.gateway import LLMGateway
from relay_core.security.rbac import Role, has_at_least

router = APIRouter(prefix="/workspaces/{workspace_id}/memories", tags=["memories"])


class MemoryOut(BaseModel):
    id: uuid.UUID
    scope: str
    kind: str
    content: str
    confidence: float
    is_active: bool
    use_count: int
    source_run_id: uuid.UUID | None

    @classmethod
    def from_model(cls, m: Memory) -> "MemoryOut":
        return cls(
            id=m.id,
            scope=m.scope,
            kind=m.kind,
            content=m.content,
            confidence=m.confidence,
            is_active=m.is_active,
            use_count=m.use_count,
            source_run_id=m.source_run_id,
        )


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1)
    kind: str | None = None
    is_active: bool | None = None


@router.get("", response_model=list[MemoryOut])
async def list_memories(
    workspace_id: uuid.UUID = Path(...),
    kind: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    q: str | None = Query(default=None, description="Case-insensitive substring of the content"),
    include_inactive: bool = Query(default=False),
    current: CurrentUser = Depends(require_workspace_role(Role.member.name)),
    session: AsyncSession = Depends(get_session),
) -> list[MemoryOut]:
    """The caller's own memories plus the workspace's — never another member's, which is the
    same rule `MemoryRepository.nearest` applies when a run retrieves them."""
    _check_kind(kind)
    rows = await MemoryRepository(session).list_visible_to(
        workspace_id, current.user.id, kind=kind, include_inactive=include_inactive
    )
    if scope is not None:
        rows = [m for m in rows if m.scope == scope]
    if q:
        needle = q.lower()
        rows = [m for m in rows if needle in m.content.lower()]
    return [MemoryOut.from_model(m) for m in rows]


@router.patch("/{memory_id}", response_model=MemoryOut)
async def update_memory(
    patch: MemoryPatch,
    workspace_id: uuid.UUID = Path(...),
    memory_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.member.name)),
    session: AsyncSession = Depends(get_session),
    gateway: LLMGateway = Depends(get_llm_gateway),
    settings: Settings = Depends(get_settings_dep),
) -> MemoryOut:
    repo = MemoryRepository(session)
    memory = await _visible_or_404(repo, workspace_id, memory_id, current)
    _check_kind(patch.kind)

    fields: dict[str, object] = {}
    if patch.kind is not None:
        fields["kind"] = patch.kind
    if patch.is_active is not None:
        fields["is_active"] = patch.is_active
    if patch.content is not None and patch.content != memory.content:
        # Retrieval is by embedding, so edited text that kept its old vector would still be
        # recalled by the old text's queries and not by its own. Re-embedding is what makes an
        # edit actually change the memory's behaviour.
        [vector] = await gateway.embed(
            [patch.content], task="RETRIEVAL_DOCUMENT", settings=settings
        )
        fields["content"] = patch.content
        fields["embedding"] = vector
        fields["embedding_model"] = settings.embedding_model

    if fields:
        memory = await repo.update(memory, **fields)
    return MemoryOut.from_model(memory)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    workspace_id: uuid.UUID = Path(...),
    memory_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role(Role.member.name)),
    session: AsyncSession = Depends(get_session),
) -> None:
    repo = MemoryRepository(session)
    await repo.delete(await _visible_or_404(repo, workspace_id, memory_id, current))
    await AuditLogRepository(session).record(
        workspace_id,
        actor_type="user",
        actor_user_id=current.user.id,
        action="memory.deleted",
        target_type="memory",
        target_id=memory_id,
    )


def _check_kind(kind: str | None) -> None:
    if kind is not None and kind not in KINDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"kind must be one of {', '.join(KINDS)}"
        )


async def _visible_or_404(
    repo: MemoryRepository, workspace_id: uuid.UUID, memory_id: uuid.UUID, current: CurrentUser
) -> Memory:
    """404 for another member's memory rather than 403: whether someone else's memory exists is
    itself something they shouldn't learn. A workspace memory is visible to every member but
    writable only by an admin, so that one *is* a 403 — the caller can already see it."""
    memory = await repo.get(workspace_id, memory_id)
    if memory is None or (memory.scope == "user" and memory.user_id != current.user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Memory not found")
    if memory.scope == "workspace" and not has_at_least(current.membership.role, Role.admin.name):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only an owner or admin can change a workspace memory"
        )
    return memory
