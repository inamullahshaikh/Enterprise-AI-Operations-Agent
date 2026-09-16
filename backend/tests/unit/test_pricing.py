from datetime import date
from decimal import Decimal

from relay_core.db.models.llm import ModelPricing
from relay_core.llm.pricing import cost_for_usage
from relay_core.llm.schemas import Usage


def _pricing(**overrides: object) -> ModelPricing:
    defaults: dict[str, object] = {
        "model": "test-model",
        "input_per_mtok": Decimal("1.0"),
        "output_per_mtok": Decimal("2.0"),
        "cached_input_per_mtok": None,
        "effective_from": date(2026, 1, 1),
    }
    defaults.update(overrides)
    return ModelPricing(**defaults)  # type: ignore[arg-type]


def test_cost_scales_with_input_and_output_tokens() -> None:
    usage = Usage(
        input_tokens=1_000_000, cached_tokens=0, output_tokens=1_000_000, thought_tokens=0
    )
    cost = cost_for_usage(usage, _pricing())
    assert cost == Decimal("3.0")  # 1M * $1/Mtok + 1M * $2/Mtok


def test_thinking_tokens_are_billed_at_the_output_rate() -> None:
    usage = Usage(input_tokens=0, cached_tokens=0, output_tokens=0, thought_tokens=1_000_000)
    cost = cost_for_usage(usage, _pricing())
    assert cost == Decimal("2.0")


def test_cached_tokens_use_the_cached_rate_when_set() -> None:
    usage = Usage(
        input_tokens=1_000_000, cached_tokens=1_000_000, output_tokens=0, thought_tokens=0
    )
    cost = cost_for_usage(usage, _pricing(cached_input_per_mtok=Decimal("0.25")))
    # All 1M input tokens are cached, at the cached rate, none at the full rate.
    assert cost == Decimal("0.25")


def test_cached_tokens_fall_back_to_input_rate_when_no_cached_rate_set() -> None:
    usage = Usage(
        input_tokens=1_000_000, cached_tokens=1_000_000, output_tokens=0, thought_tokens=0
    )
    cost = cost_for_usage(usage, _pricing(cached_input_per_mtok=None))
    assert cost == Decimal("1.0")


def test_zero_usage_is_zero_cost() -> None:
    usage = Usage(input_tokens=0, cached_tokens=0, output_tokens=0, thought_tokens=0)
    assert cost_for_usage(usage, _pricing()) == Decimal("0")
