import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from relay_core.db.models.tool_calls import ToolCall
from relay_core.db.repositories.base import WorkspaceScopedRepository


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
        call = await self.get(workspace_id, id_)
        if call is None:
            raise ValueError(f"tool_calls row {id_} not found in workspace {workspace_id}")
        call.status = "succeeded" if ok else "failed"
        call.output = output
        call.error = error
        call.latency_ms = latency_ms
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
