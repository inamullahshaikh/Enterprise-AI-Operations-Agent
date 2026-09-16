import uuid
from datetime import UTC, datetime
from typing import Any

from relay_core.db.models.runs import AgentRun
from relay_core.db.repositories.base import WorkspaceScopedRepository
from relay_core.db.repositories.llm_calls import UsageTotals


class AgentRunRepository(WorkspaceScopedRepository[AgentRun]):
    model = AgentRun

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        trigger_message_id: uuid.UUID | None,
    ) -> AgentRun:
        run = AgentRun(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            user_id=user_id,
            trigger_message_id=trigger_message_id,
            status="queued",
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def mark_running(self, workspace_id: uuid.UUID, id_: uuid.UUID) -> AgentRun:
        run = await self._require(workspace_id, id_)
        run.status = "running"
        run.started_at = datetime.now(UTC)
        return run

    async def mark_completed(
        self,
        workspace_id: uuid.UUID,
        id_: uuid.UUID,
        *,
        final_message_id: uuid.UUID,
        route: str | None,
        usage: UsageTotals,
        tool_calls: int = 0,
        capability_snapshot: list[str] | None = None,
    ) -> AgentRun:
        run = await self._require(workspace_id, id_)
        run.status = "completed"
        run.route = route
        run.final_message_id = final_message_id
        run.tool_calls = tool_calls
        run.capability_snapshot = capability_snapshot
        self._apply_usage(run, usage)
        run.finished_at = datetime.now(UTC)
        return run

    async def mark_awaiting_input(
        self,
        workspace_id: uuid.UUID,
        id_: uuid.UUID,
        *,
        final_message_id: uuid.UUID,
        route: str | None,
        missing_capabilities: dict[str, Any] | None,
        usage: UsageTotals,
        tool_calls: int = 0,
        capability_snapshot: list[str] | None = None,
    ) -> AgentRun:
        run = await self._require(workspace_id, id_)
        run.status = "awaiting_input"
        run.route = route
        run.missing_capabilities = missing_capabilities
        run.final_message_id = final_message_id
        run.tool_calls = tool_calls
        run.capability_snapshot = capability_snapshot
        self._apply_usage(run, usage)
        run.finished_at = datetime.now(UTC)
        return run

    async def mark_failed(
        self, workspace_id: uuid.UUID, id_: uuid.UUID, *, error_code: str, error_message: str
    ) -> AgentRun:
        run = await self._require(workspace_id, id_)
        run.status = "failed"
        run.error_code = error_code
        run.error_message = error_message
        run.finished_at = datetime.now(UTC)
        return run

    async def set_plan(self, workspace_id: uuid.UUID, id_: uuid.UUID, plan: dict[str, Any]) -> None:
        run = await self._require(workspace_id, id_)
        run.plan = plan

    def _apply_usage(self, run: AgentRun, usage: UsageTotals) -> None:
        run.llm_calls = usage.llm_calls
        run.input_tokens = usage.input_tokens
        run.output_tokens = usage.output_tokens
        run.thought_tokens = usage.thought_tokens
        run.cost_usd = usage.cost_usd

    async def _require(self, workspace_id: uuid.UUID, id_: uuid.UUID) -> AgentRun:
        run = await self.get(workspace_id, id_)
        if run is None:
            raise ValueError(f"agent_runs row {id_} not found in workspace {workspace_id}")
        return run
