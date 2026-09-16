"""The LLM gateway (docs/system-design.md section 9.1).

Every call to Gemini goes through `LLMGateway.generate()` — nothing else imports
`google.genai`. That's what makes it possible to intercept every call for
budgeting, approval-relevant logging, and structured-output validation, and it's
why automatic function calling is disabled: Relay must see and gate every
function call itself rather than let the SDK execute one automatically.
"""

import time
import uuid
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from relay_core.config import Settings
from relay_core.db.repositories.llm_calls import LLMCallRepository, ModelPricingRepository
from relay_core.llm.pricing import UnknownModelPricing, cost_for_usage
from relay_core.llm.profiles import get_profile
from relay_core.llm.ratelimit import RedisRateLimiter
from relay_core.llm.schemas import LLMResponse, Usage

_RETRYABLE = (genai.errors.ServerError,)


class LLMGateway:
    def __init__(
        self,
        client: genai.Client,
        *,
        limiter: RedisRateLimiter,
        llm_calls: LLMCallRepository,
        pricing: ModelPricingRepository,
    ) -> None:
        self.client = client
        self.limiter = limiter
        self.llm_calls = llm_calls
        self.pricing = pricing

    async def generate(
        self,
        *,
        role: str,
        system: str,
        contents: Any,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        settings: Settings | None = None,
    ) -> LLMResponse:
        profile = get_profile(role, settings=settings)
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=tools)] if tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            response_mime_type="application/json" if response_schema else None,
            response_schema=response_schema,
            thinking_config=(
                types.ThinkingConfig(thinking_level=profile.thinking_level)
                if profile.thinking_level
                else None
            ),
            temperature=profile.temperature,
            max_output_tokens=profile.max_output_tokens,
        )

        model = profile.model
        fallback_from: str | None = None
        await self.limiter.acquire(model)
        started = time.monotonic()
        try:
            resp = await self._call_with_retry(model, contents, config)
        except genai.errors.ClientError as exc:
            # Errors that survive retry only trigger a fallback on resource
            # exhaustion (docs/system-design.md section 9.3); anything else
            # (bad request, safety block, ...) would fail identically on a
            # different model, so it propagates as-is.
            if not (profile.fallback_model and _is_resource_exhausted(exc)):
                raise
            fallback_from, model = model, profile.fallback_model
            await self.limiter.acquire(model)
            resp = await self._call_with_retry(model, contents, config)

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = _usage_from_sdk(resp)
        pricing_row = await self.pricing.get(model)
        if pricing_row is None:
            raise UnknownModelPricing(f"No model_pricing row for {model!r}; seed it via migration")
        cost = cost_for_usage(usage, pricing_row)

        await self.llm_calls.create(
            workspace_id=workspace_id,
            run_id=run_id,
            node=role,
            model=model,
            fallback_from=fallback_from,
            thinking_level=profile.thinking_level,
            input_tokens=usage.input_tokens,
            cached_tokens=usage.cached_tokens,
            output_tokens=usage.output_tokens,
            thought_tokens=usage.thought_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            finish_reason=(resp.candidates[0].finish_reason if resp.candidates else None),
            status="ok",
            error=None,
        )

        return LLMResponse.from_sdk(
            resp, model=model, fallback_from=fallback_from, cost_usd=cost, latency_ms=latency_ms
        )

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.5, max=8),
        reraise=True,
    )
    async def _call_with_retry(
        self, model: str, contents: Any, config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        return await self.client.aio.models.generate_content(
            model=model, contents=contents, config=config
        )


def _is_resource_exhausted(exc: "genai.errors.ClientError") -> bool:
    return getattr(exc, "code", None) == 429


def _usage_from_sdk(resp: types.GenerateContentResponse) -> Usage:
    md = resp.usage_metadata
    return Usage(
        input_tokens=(md.prompt_token_count or 0) if md else 0,
        cached_tokens=(md.cached_content_token_count or 0) if md else 0,
        output_tokens=(md.candidates_token_count or 0) if md else 0,
        thought_tokens=(md.thoughts_token_count or 0) if md else 0,
    )
