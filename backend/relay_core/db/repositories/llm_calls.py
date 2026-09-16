import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.llm import LLMCall, ModelPricing
from relay_core.db.repositories.base import WorkspaceScopedRepository


@dataclass(frozen=True)
class UsageTotals:
    llm_calls: int
    input_tokens: int
    output_tokens: int
    thought_tokens: int
    cost_usd: Decimal


class LLMCallRepository(WorkspaceScopedRepository[LLMCall]):
    model = LLMCall

    async def create(self, *, workspace_id: uuid.UUID, **fields: object) -> LLMCall:
        call = LLMCall(workspace_id=workspace_id, **fields)
        self.session.add(call)
        await self.session.flush()
        return call

    async def sum_usage_for_run(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> UsageTotals:
        """Rolls the `llm_calls` rows a run produced up into one summary
        (docs/system-design.md section 14.3: `agent_runs.llm_calls`/`*_tokens`/
        `cost_usd` are denormalized aggregates for cheap listing/dashboards —
        this is the one place that computes them, so `finalize`/`ask_missing`
        never do the arithmetic themselves)."""
        stmt = select(
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.input_tokens), 0),
            func.coalesce(func.sum(LLMCall.output_tokens), 0),
            func.coalesce(func.sum(LLMCall.thought_tokens), 0),
            func.coalesce(func.sum(LLMCall.cost_usd), Decimal(0)),
        ).where(LLMCall.workspace_id == workspace_id, LLMCall.run_id == run_id)
        row = (await self.session.execute(stmt)).one()
        return UsageTotals(
            llm_calls=row[0],
            input_tokens=row[1],
            output_tokens=row[2],
            thought_tokens=row[3],
            cost_usd=row[4],
        )


class ModelPricingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, model: str) -> ModelPricing | None:
        return await self.session.get(ModelPricing, model)

    async def get_all(self) -> dict[str, ModelPricing]:
        stmt = select(ModelPricing)
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.model: row for row in rows}
