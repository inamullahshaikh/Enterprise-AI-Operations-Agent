import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Select, SQLColumnExpression, func, select
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


@dataclass(frozen=True)
class UsageBreakdown:
    """One aggregated row per group (day/model/node)."""

    group_key: str
    llm_calls: int
    input_tokens: int
    output_tokens: int
    thought_tokens: int
    cached_tokens: int
    cost_usd: Decimal


def month_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """[first of this month, first of next month) in UTC: the default usage window, and the one
    the monthly budget (Phase 8 B2) is checked against."""
    start = (now or datetime.now(UTC)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


class LLMCallRepository(WorkspaceScopedRepository[LLMCall]):
    model = LLMCall

    async def create(self, *, workspace_id: uuid.UUID, **fields: object) -> LLMCall:
        call = LLMCall(workspace_id=workspace_id, **fields)
        self.session.add(call)
        await self.session.flush()
        return call

    async def count_for_node(self, workspace_id: uuid.UUID, run_id: uuid.UUID, node: str) -> int:
        """How many calls one graph node made during a run. `llm_calls.node` records the gateway
        `role`, and `plan` and `replan` share the `planner` role — so two `planner` calls in one
        run is exactly "the plan was revised once", which is what the `planning` eval suite
        scores on without needing a new column (docs/system-design.md section 21.1)."""
        stmt = select(func.count(LLMCall.id)).where(
            LLMCall.workspace_id == workspace_id,
            LLMCall.run_id == run_id,
            LLMCall.node == node,
        )
        return int((await self.session.execute(stmt)).scalar_one())

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

    async def usage_breakdown(
        self,
        workspace_id: uuid.UUID,
        *,
        from_: datetime | None = None,
        to_: datetime | None = None,
        group_by: Literal["day", "model", "node"] = "day",
    ) -> list[UsageBreakdown]:
        """Aggregates `llm_calls` per day, model or node in one query. Omitted bounds default
        to the current calendar month (docs/system-design.md section 15.5)."""
        default_from, default_to = month_window()
        from_, to_ = from_ or default_from, to_ or default_to

        group_expr: SQLColumnExpression[Any] = {
            "day": func.date(LLMCall.created_at),
            "model": LLMCall.model,
            "node": LLMCall.node,
        }[group_by]

        stmt: Select[Any] = (
            select(
                group_expr.label("group_key"),
                func.count(LLMCall.id),
                func.coalesce(func.sum(LLMCall.input_tokens), 0),
                func.coalesce(func.sum(LLMCall.output_tokens), 0),
                func.coalesce(func.sum(LLMCall.thought_tokens), 0),
                func.coalesce(func.sum(LLMCall.cached_tokens), 0),
                func.coalesce(func.sum(LLMCall.cost_usd), Decimal(0)),
            )
            .where(
                LLMCall.workspace_id == workspace_id,
                LLMCall.created_at >= from_,
                LLMCall.created_at < to_,
            )
            .group_by("group_key")
            .order_by("group_key")
        )
        rows = await self.session.execute(stmt)
        return [
            UsageBreakdown(
                group_key=str(row[0]),
                llm_calls=row[1],
                input_tokens=row[2],
                output_tokens=row[3],
                thought_tokens=row[4],
                cached_tokens=row[5],
                cost_usd=row[6],
            )
            for row in rows
        ]


class ModelPricingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, model: str) -> ModelPricing | None:
        return await self.session.get(ModelPricing, model)

    async def get_all(self) -> dict[str, ModelPricing]:
        stmt = select(ModelPricing)
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.model: row for row in rows}
