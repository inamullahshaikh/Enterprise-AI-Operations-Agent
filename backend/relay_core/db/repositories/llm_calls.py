import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay_core.db.models.llm import LLMCall, ModelPricing
from relay_core.db.repositories.base import WorkspaceScopedRepository


class LLMCallRepository(WorkspaceScopedRepository[LLMCall]):
    model = LLMCall

    async def create(self, *, workspace_id: uuid.UUID, **fields: object) -> LLMCall:
        call = LLMCall(workspace_id=workspace_id, **fields)
        self.session.add(call)
        await self.session.flush()
        return call


class ModelPricingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, model: str) -> ModelPricing | None:
        return await self.session.get(ModelPricing, model)

    async def get_all(self) -> dict[str, ModelPricing]:
        stmt = select(ModelPricing)
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.model: row for row in rows}
