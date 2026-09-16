"""Shared dataclasses for the LLM gateway (docs/system-design.md sections 5.2, 9.1)."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from google.genai import types
from pydantic import BaseModel


@dataclass(frozen=True)
class ModelProfile:
    model: str
    thinking_level: str | None  # "minimal" | "low" | "medium" | "high"
    temperature: float | None = None
    max_output_tokens: int | None = None
    fallback_model: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    thought_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    text: str | None
    # Matches `GenerateContentResponse.parsed`'s own type: populated only when a
    # Pydantic `response_schema` was passed and the SDK could parse the reply.
    parsed: BaseModel | dict[str, Any] | Enum | None
    function_calls: list[types.FunctionCall]
    raw_content: types.Content | None
    finish_reason: str | None
    usage: Usage
    model: str
    fallback_from: str | None
    cost_usd: Decimal
    latency_ms: int

    @classmethod
    def from_sdk(
        cls,
        resp: types.GenerateContentResponse,
        *,
        model: str,
        fallback_from: str | None,
        cost_usd: Decimal,
        latency_ms: int,
    ) -> "LLMResponse":
        usage_md = resp.usage_metadata
        usage = Usage(
            input_tokens=(usage_md.prompt_token_count or 0) if usage_md else 0,
            cached_tokens=(usage_md.cached_content_token_count or 0) if usage_md else 0,
            output_tokens=(usage_md.candidates_token_count or 0) if usage_md else 0,
            thought_tokens=(usage_md.thoughts_token_count or 0) if usage_md else 0,
        )
        candidates = resp.candidates or []
        finish_reason = (
            str(candidates[0].finish_reason) if candidates and candidates[0].finish_reason else None
        )
        raw_content = candidates[0].content if candidates else None
        return cls(
            text=resp.text,
            parsed=resp.parsed,
            function_calls=resp.function_calls or [],
            raw_content=raw_content,
            finish_reason=finish_reason,
            usage=usage,
            model=model,
            fallback_from=fallback_from,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )


def parse_structured[T: BaseModel](resp: LLMResponse, schema: type[T]) -> T | None:
    """Best-effort parse of a structured-output response into `schema`. Prefers
    the SDK's own `resp.parsed` when it already matches — a genuine round-trip
    against the real API populates it — and otherwise falls back to validating
    `resp.text` directly, which is what a fake client in a test (or a future
    non-Gemini provider behind this same gateway) can reliably provide.
    """
    if isinstance(resp.parsed, schema):
        return resp.parsed
    if resp.text:
        try:
            return schema.model_validate_json(resp.text)
        except ValueError:
            return None
    return None
