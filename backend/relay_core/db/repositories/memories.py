"""Long-term memory rows (docs/system-design.md section 12).

`nearest` is what `load_context` calls on every run, so the scope rule lives here rather than in
each caller: a user sees their own memories plus the workspace's, never another user's.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Select, or_, select, update

from relay_core.db.models.memories import Memory
from relay_core.db.repositories.base import WorkspaceScopedRepository


class MemoryRepository(WorkspaceScopedRepository[Memory]):
    model = Memory

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID | None,
        scope: str,
        kind: str,
        content: str,
        confidence: float,
        embedding: list[float],
        embedding_model: str,
        source_run_id: uuid.UUID | None = None,
    ) -> Memory:
        memory = Memory(
            workspace_id=workspace_id,
            user_id=None if scope == "workspace" else user_id,
            scope=scope,
            kind=kind,
            content=content,
            confidence=confidence,
            embedding=embedding,
            embedding_model=embedding_model,
            source_run_id=source_run_id,
        )
        self.session.add(memory)
        await self.session.flush()
        return memory

    async def list_visible_to(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        kind: str | None = None,
        include_inactive: bool = False,
    ) -> list[Memory]:
        stmt = self._visible(workspace_id, user_id)
        if kind is not None:
            stmt = stmt.where(Memory.kind == kind)
        if not include_inactive:
            stmt = stmt.where(Memory.is_active.is_(True))
        return list((await self.session.execute(stmt.order_by(Memory.created_at.desc()))).scalars())

    async def nearest(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        vector: list[float],
        *,
        limit: int = 5,
        max_distance: float = 0.5,
    ) -> list[Memory]:
        """The closest active memories visible to this user. `max_distance` is cosine distance,
        so a smaller number is a closer match; anything beyond it is unrelated text that would
        only add noise to the prompt."""
        distance = Memory.embedding.cosine_distance(vector)
        stmt = (
            self._visible(workspace_id, user_id)
            .where(Memory.is_active.is_(True), distance <= max_distance)
            .order_by(distance)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def mark_used(self, workspace_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
        if not ids:
            return
        await self.session.execute(
            update(Memory)
            .where(Memory.workspace_id == workspace_id, Memory.id.in_(ids))
            .values(use_count=Memory.use_count + 1, last_used_at=datetime.now(UTC))
        )

    async def update(self, memory: Memory, **fields: object) -> Memory:
        for key, value in fields.items():
            setattr(memory, key, value)
        await self.session.flush()
        return memory

    async def delete(self, memory: Memory) -> None:
        await self.session.delete(memory)
        await self.session.flush()

    def _visible(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> Select[tuple[Memory]]:
        return select(Memory).where(
            Memory.workspace_id == workspace_id,
            or_(Memory.user_id == user_id, Memory.scope == "workspace"),
        )
