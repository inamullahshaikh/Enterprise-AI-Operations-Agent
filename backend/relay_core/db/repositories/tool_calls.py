import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from relay_core.db.base import uuid7
from relay_core.db.models.connectors import ConnectorInstallation
from relay_core.db.models.tool_calls import ToolCall
from relay_core.db.repositories.base import WorkspaceScopedRepository
from relay_core.db.repositories.llm_calls import month_window


@dataclass(frozen=True)
class ToolReliability:
    """Tool call count for one (connector, status) pair."""

    connector_key: str
    status: str
    calls: int


def idempotency_key_for(tool_call_id: uuid.UUID) -> str:
    """docs/system-design.md section 13.3 writes this as `sha256(approval_id)`, which is exact
    only while an approval gates a single call. Batch approvals gate many (section 13.4), so the
    key is derived from the `tool_calls` row instead — that row *is* the individual proposed
    call, it is created once before the interrupt, and its id survives the checkpoint, so it
    identifies "this exact write" across a crash and resume.

    It stays a hash rather than the bare uuid because connectors forward it to third parties as
    an `Idempotency-Key` header, and an internal primary key shouldn't leave the system.
    """
    return hashlib.sha256(str(tool_call_id).encode()).hexdigest()


class ToolCallRepository(WorkspaceScopedRepository[ToolCall]):
    model = ToolCall

    async def start(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        plan_step_id: str,
        installation_id: uuid.UUID | None,
        llm_name: str,
        arguments: dict[str, Any],
        risk: str,
    ) -> ToolCall:
        call = ToolCall(
            workspace_id=workspace_id,
            run_id=run_id,
            plan_step_id=plan_step_id,
            installation_id=installation_id,
            llm_name=llm_name,
            arguments=arguments,
            risk=risk,
            status="running",
            started_at=datetime.now(UTC),
        )
        self.session.add(call)
        await self.session.flush()
        return call

    async def finish(
        self,
        workspace_id: uuid.UUID,
        id_: uuid.UUID,
        *,
        ok: bool,
        output: dict[str, Any] | None,
        error: str | None,
        latency_ms: int,
    ) -> ToolCall:
        call = await self._require(workspace_id, id_)
        call.status = "succeeded" if ok else "failed"
        call.output = output
        call.error = error
        call.latency_ms = latency_ms
        call.finished_at = datetime.now(UTC)
        return call

    async def annotate_output(
        self, workspace_id: uuid.UUID, id_: uuid.UUID, key: str, value: Any
    ) -> None:
        call = await self._require(workspace_id, id_)
        # A new dict, not an in-place edit: SQLAlchemy does not see mutations inside JSONB.
        call.output = {**(call.output or {}), key: value}

    async def create_pending_approval(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        plan_step_id: str,
        installation_id: uuid.UUID | None,
        llm_name: str,
        arguments: dict[str, Any],
        risk: str,
    ) -> ToolCall:
        """A write call the model proposed but nobody has approved yet — the row exists before
        the decision so the approval can point at it and so the audit trail records what was
        asked for even if it is ultimately rejected (docs/system-design.md section 13).

        The id is generated here rather than left to the column default because the
        `idempotency_key` is derived from it (see `idempotency_key_for`), and both have to land
        in the same INSERT.
        """
        call_id = uuid7()
        call = ToolCall(
            id=call_id,
            workspace_id=workspace_id,
            run_id=run_id,
            plan_step_id=plan_step_id,
            installation_id=installation_id,
            llm_name=llm_name,
            arguments=arguments,
            risk=risk,
            status="pending_approval",
            idempotency_key=idempotency_key_for(call_id),
        )
        self.session.add(call)
        await self.session.flush()
        return call

    async def succeeded_output(
        self, workspace_id: uuid.UUID, id_: uuid.UUID
    ) -> dict[str, Any] | None:
        """The stored result of an already-successful call, or None if it hasn't run yet.

        This is the lookup docs/system-design.md section 13.3 describes as "check `tool_calls`
        for an existing successful call with the same key": because `idempotency_key` is derived
        from this row's own id, asking whether *this row* already succeeded and asking whether
        anything with that key already succeeded are the same question — and asking it by
        primary key avoids a second index lookup.
        """
        call = await self.get(workspace_id, id_)
        if call is None or call.status != "succeeded":
            return None
        return call.output

    async def mark_running(self, workspace_id: uuid.UUID, id_: uuid.UUID) -> ToolCall:
        call = await self._require(workspace_id, id_)
        call.status = "running"
        call.started_at = datetime.now(UTC)
        return call

    async def mark_not_executed(
        self, workspace_id: uuid.UUID, id_: uuid.UUID, *, status: str, reason: str | None = None
    ) -> ToolCall:
        """Closes out a proposed write that never ran — `rejected` when the approver said no,
        `skipped` when they unticked it from a batch or the approval expired."""
        call = await self._require(workspace_id, id_)
        call.status = status
        call.error = reason
        call.finished_at = datetime.now(UTC)
        return call

    async def count_for_run(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> int:
        stmt = select(func.count(ToolCall.id)).where(
            ToolCall.workspace_id == workspace_id, ToolCall.run_id == run_id
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_for_run(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> list[ToolCall]:
        """Used by the eval harness (`relay_eval.scoring`) to inspect what a run actually
        called — e.g. the `text_to_sql` suite's execution-accuracy check reads `run_sql`
        outputs straight from here rather than re-deriving them from `agent_runs.plan`."""
        stmt = (
            select(ToolCall)
            .where(ToolCall.workspace_id == workspace_id, ToolCall.run_id == run_id)
            .order_by(ToolCall.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def reliability_breakdown(
        self,
        workspace_id: uuid.UUID,
        *,
        from_: datetime | None = None,
        to_: datetime | None = None,
    ) -> list[ToolReliability]:
        """Tool calls counted per connector and status (section 20.3's "tool reliability by
        connector"). Calls with no installation (`file_upload`, or an uninstalled connector)
        count under "none". Omitted bounds default to the current calendar month."""
        default_from, default_to = month_window()
        from_, to_ = from_ or default_from, to_ or default_to
        connector = func.coalesce(ConnectorInstallation.connector_key, "none").label("connector")
        stmt = (
            select(connector, ToolCall.status, func.count())
            .select_from(ToolCall)
            .outerjoin(ConnectorInstallation, ConnectorInstallation.id == ToolCall.installation_id)
            .where(
                ToolCall.workspace_id == workspace_id,
                ToolCall.created_at >= from_,
                ToolCall.created_at < to_,
            )
            .group_by(connector, ToolCall.status)
            .order_by(connector, ToolCall.status)
        )
        rows = await self.session.execute(stmt)
        return [ToolReliability(connector_key=r[0], status=r[1], calls=r[2]) for r in rows]

    async def _require(self, workspace_id: uuid.UUID, id_: uuid.UUID) -> ToolCall:
        call = await self.get(workspace_id, id_)
        if call is None:
            raise ValueError(f"tool_calls row {id_} not found in workspace {workspace_id}")
        return call
