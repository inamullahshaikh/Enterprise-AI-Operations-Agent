"""Manual verification endpoint for the Phase 1 "done when" (docs/system-design.md
section 28): "a test endpoint calls Gemini with a Pydantic schema and the call
appears in `llm_calls`." Not part of the documented API surface (section 15) and
mounted only outside `prod` — it exists to let a developer curl the LLM gateway
end-to-end against a real `GEMINI_API_KEY`, not for product use.
"""

import uuid

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel

from relay_api.deps import (
    CurrentUser,
    get_llm_gateway,
    get_settings_dep,
    require_non_prod,
    require_workspace_role,
)
from relay_core.config import Settings
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.profiles import LIGHT

router = APIRouter(
    prefix="/workspaces/{workspace_id}/_debug",
    tags=["debug"],
    dependencies=[Depends(require_non_prod)],
)


class GreetingResult(BaseModel):
    greeting: str
    language: str


class GeminiPingResponse(BaseModel):
    parsed: GreetingResult | None
    text: str | None
    model: str
    cost_usd: str
    latency_ms: int


@router.post("/gemini-ping", response_model=GeminiPingResponse)
async def gemini_ping(
    workspace_id: uuid.UUID = Path(...),
    current: CurrentUser = Depends(require_workspace_role("viewer")),
    gateway: LLMGateway = Depends(get_llm_gateway),
    settings: Settings = Depends(get_settings_dep),
) -> GeminiPingResponse:
    resp = await gateway.generate(
        role=LIGHT,
        system="Reply with a short, friendly greeting in the requested language.",
        contents="Greet me in French.",
        workspace_id=workspace_id,
        response_schema=GreetingResult,
        settings=settings,
    )
    parsed = resp.parsed if isinstance(resp.parsed, GreetingResult) else None
    return GeminiPingResponse(
        parsed=parsed,
        text=resp.text,
        model=resp.model,
        cost_usd=str(resp.cost_usd),
        latency_ms=resp.latency_ms,
    )
