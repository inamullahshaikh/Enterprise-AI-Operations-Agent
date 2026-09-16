import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest
from google.genai import types
from pydantic import BaseModel

from relay_core.llm.gateway import LLMGateway
from relay_core.llm.pricing import UnknownModelPricing
from relay_core.llm.profiles import LIGHT


def _response(
    text: str, *, prompt_tokens=10, output_tokens=5, thought_tokens=0
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=text)]),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
            thoughts_token_count=thought_tokens,
            cached_content_token_count=0,
        ),
    )


class FakeModels:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeAio:
    models: FakeModels


@dataclass
class FakeClient:
    aio: FakeAio


class FakeLimiter:
    def __init__(self) -> None:
        self.acquired: list[str] = []

    async def acquire(self, model: str) -> None:
        self.acquired.append(model)


class FakePricingRepo:
    def __init__(self, prices: dict[str, Decimal]) -> None:
        self._prices = prices

    async def get(self, model: str):
        from relay_core.db.models.llm import ModelPricing

        if model not in self._prices:
            return None
        rate = self._prices[model]
        return ModelPricing(
            model=model,
            input_per_mtok=rate,
            output_per_mtok=rate,
            cached_input_per_mtok=None,
            effective_from=date(2026, 1, 1),
        )


@dataclass
class FakeLLMCallsRepo:
    rows: list[dict] = field(default_factory=list)

    async def create(self, **kwargs):
        self.rows.append(kwargs)
        return kwargs


class Ping(BaseModel):
    message: str


def _gateway(client: FakeClient, *, pricing: dict[str, Decimal] | None = None):
    llm_calls = FakeLLMCallsRepo()
    if pricing is None:
        pricing = {"gemini-3.5-flash-lite": Decimal("1.0")}
    pricing_repo = FakePricingRepo(pricing)
    limiter = FakeLimiter()
    gateway = LLMGateway(client, limiter=limiter, llm_calls=llm_calls, pricing=pricing_repo)
    return gateway, llm_calls, limiter


@pytest.mark.asyncio
async def test_generate_records_usage_and_cost(test_settings) -> None:
    client = FakeClient(aio=FakeAio(models=FakeModels([_response('{"message":"hi"}')])))
    gateway, llm_calls, limiter = _gateway(client)

    resp = await gateway.generate(
        role=LIGHT,
        system="s",
        contents="c",
        workspace_id=uuid.uuid4(),
        response_schema=Ping,
        settings=test_settings,
    )

    assert resp.text == '{"message":"hi"}'
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 5
    assert len(llm_calls.rows) == 1
    assert llm_calls.rows[0]["model"] == "gemini-3.5-flash-lite"
    assert llm_calls.rows[0]["status"] == "ok"
    assert limiter.acquired == ["gemini-3.5-flash-lite"]


@pytest.mark.asyncio
async def test_missing_pricing_row_raises(test_settings) -> None:
    client = FakeClient(aio=FakeAio(models=FakeModels([_response('{"message":"hi"}')])))
    gateway, _, _ = _gateway(client, pricing={})

    with pytest.raises(UnknownModelPricing):
        await gateway.generate(
            role=LIGHT, system="s", contents="c", workspace_id=uuid.uuid4(), settings=test_settings
        )


@pytest.mark.asyncio
async def test_resource_exhausted_falls_back_to_configured_fallback_model(test_settings) -> None:
    # Only the `executor` role profile carries a `fallback_model`
    # (relay_core.llm.profiles), so this exercises EXECUTOR rather than LIGHT.
    from google.genai import errors as genai_errors

    from relay_core.llm.profiles import EXECUTOR

    exhausted = genai_errors.ClientError(429, {"error": {"message": "quota"}})
    client = FakeClient(
        aio=FakeAio(models=FakeModels([exhausted, _response('{"message":"fallback"}')]))
    )
    gateway, llm_calls, limiter = _gateway(
        client,
        pricing={
            test_settings.model_executor: Decimal("1.0"),
            test_settings.model_fallback_executor: Decimal("1.0"),
        },
    )

    resp = await gateway.generate(
        role=EXECUTOR, system="s", contents="c", workspace_id=uuid.uuid4(), settings=test_settings
    )

    assert resp.model == test_settings.model_fallback_executor
    assert resp.fallback_from == test_settings.model_executor
    assert limiter.acquired == [test_settings.model_executor, test_settings.model_fallback_executor]
    assert llm_calls.rows[0]["fallback_from"] == test_settings.model_executor
