"""Model profiles per role (docs/system-design.md sections 5.2 and 25).

Model names and thinking levels are configuration, not code — see the note in
section 5.2: model names change fast, so nothing here is hardcoded.
"""

from relay_core.config import Settings, get_settings
from relay_core.llm.schemas import ModelProfile

# Role names used as the `role=` argument to `LLMGateway.generate()`.
PLANNER = "planner"
EXECUTOR = "executor"
VALIDATOR = "validator"
LIGHT = "light"


def _profiles_for(settings: Settings) -> dict[str, ModelProfile]:
    return {
        PLANNER: ModelProfile(
            model=settings.model_planner, thinking_level=settings.model_planner_thinking
        ),
        EXECUTOR: ModelProfile(
            model=settings.model_executor,
            thinking_level=settings.model_executor_thinking,
            fallback_model=settings.model_fallback_executor,
        ),
        VALIDATOR: ModelProfile(
            model=settings.model_validator, thinking_level=settings.model_validator_thinking
        ),
        LIGHT: ModelProfile(
            model=settings.model_light, thinking_level=settings.model_light_thinking
        ),
    }


def get_profile(role: str, *, settings: Settings | None = None) -> ModelProfile:
    settings = settings or get_settings()
    try:
        return _profiles_for(settings)[role]
    except KeyError as exc:
        raise ValueError(f"Unknown LLM role {role!r}; add it to relay_core.llm.profiles") from exc
