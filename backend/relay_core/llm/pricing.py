"""Cost calculation against the `model_pricing` table (docs/system-design.md
section 9.1: pricing lives in config data, not code, because prices change).
"""

from decimal import Decimal

from relay_core.db.models.llm import ModelPricing
from relay_core.llm.schemas import Usage


class UnknownModelPricing(Exception):
    """Raised when a model has no row in `model_pricing`. Cost tracking is a hard
    requirement (docs/system-design.md G5), so an unpriced model fails loudly
    instead of silently recording a $0 cost."""


def cost_for_usage(usage: Usage, pricing: ModelPricing) -> Decimal:
    million = Decimal(1_000_000)
    billable_input = usage.input_tokens - usage.cached_tokens
    cached_rate = (
        pricing.cached_input_per_mtok
        if pricing.cached_input_per_mtok is not None
        else pricing.input_per_mtok
    )
    input_cost = (Decimal(billable_input) * pricing.input_per_mtok / million) + (
        Decimal(usage.cached_tokens) * cached_rate / million
    )
    # Thinking tokens are billed as output tokens (section 5.2 / 9.4).
    output_tokens_billed = Decimal(usage.output_tokens + usage.thought_tokens)
    output_cost = output_tokens_billed * pricing.output_per_mtok / million
    return input_cost + output_cost
