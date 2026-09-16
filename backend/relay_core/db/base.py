"""Declarative base and shared column mixins.

See docs/system-design.md section 14.1 for the conventions these encode:
UUIDv7 primary keys (time-ordered, so B-tree indexes on `id` stay append-mostly),
`timestamptz` created_at/updated_at on every table, and a `workspace_id` column
on every tenant table.
"""

import os
import time
import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (RFC 9562): a millisecond timestamp followed by random bits.

    Python's stdlib `uuid` module doesn't grow a `uuid7()` helper until 3.14, and this
    project targets 3.12, so it's implemented directly rather than pulling in a
    dependency for a dozen lines of bit-twiddling.
    """
    unix_ts_ms = time.time_ns() // 1_000_000
    tail = bytearray(os.urandom(10))
    tail[0] = (tail[0] & 0x0F) | 0x70  # version 7
    tail[2] = (tail[2] & 0x3F) | 0x80  # variant RFC 4122
    return uuid.UUID(bytes=unix_ts_ms.to_bytes(6, "big") + bytes(tail))


class Base(DeclarativeBase):
    # Every `Mapped[datetime]` column defaults to `timestamptz` (section 14.1's
    # "Timestamps: created_at, updated_at as timestamptz" applies to every
    # datetime column, not just those two). Without this, a bare `Mapped[datetime]`
    # falls back to a naive `TIMESTAMP` column, and asyncpg then rejects the
    # timezone-aware `datetime.now(UTC)` values the rest of the codebase uses.
    type_annotation_map = {datetime: DateTime(timezone=True)}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WorkspaceScoped:
    """Marks a model as tenant data. Every such model gets an indexed `workspace_id`.

    `relay_core.db.repositories.base.WorkspaceScopedRepository` requires a
    `workspace_id` argument on every query method for models using this mixin, so
    forgetting the tenant filter is a type error instead of a cross-tenant data leak
    (docs/system-design.md section 14.4).
    """

    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
