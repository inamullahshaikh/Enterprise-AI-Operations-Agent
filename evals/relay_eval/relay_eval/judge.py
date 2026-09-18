"""The fabrication judge for `capability_detection` (docs/system-design.md sections 21.1, 21.4;
Phase 8 D2).

One `VALIDATOR`-profile call with the rubric in `evals/judges/fabrication.md`. It asks the same
question `validate_final` asks inside the run, but with a model pinned by `MODEL_EVAL_JUDGE`, so
a model upgrade in production does not silently move the scores, and a bug that makes the two
agree shows up as both failing.

The judge is only trusted after calibration against hand labels (`relay-eval calibrate`, which
reports Cohen's kappa). Until then its gate is off: a verdict is reported, not enforced. See
`--fabrication-gate`.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from relay_core.config import Settings
from relay_core.db.models.tool_calls import ToolCall
from relay_core.llm.gateway import LLMGateway
from relay_core.llm.profiles import VALIDATOR
from relay_core.llm.schemas import parse_structured

_RUBRIC = Path(__file__).resolve().parents[2] / "judges" / "fabrication.md"
# What the judge sees of one tool result. Enough for any fixture the suites use.
_MAX_EVIDENCE_CHARS = 4_000


class FabricationVerdict(BaseModel):
    verdict: Literal["supported", "fabricated"]
    claims: list[str] = Field(default_factory=list)
    reason: str = ""


# (request, answer, evidence) -> verdict. `None` means the judge could not answer.
Judge = Callable[[str, str, list[str]], Awaitable[FabricationVerdict | None]]


def evidence_from(tool_calls: list[ToolCall]) -> list[str]:
    return [
        f"{c.llm_name}: {json.dumps(c.output, default=str)[:_MAX_EVIDENCE_CHARS]}"
        for c in tool_calls
        if c.status == "succeeded" and c.output
    ]


def build_judge(
    gateway: LLMGateway, settings: Settings, workspace_id: uuid.UUID, run_id: uuid.UUID | None
) -> Judge:
    pinned = settings.model_copy(update={"model_validator": settings.model_eval_judge})
    rubric = _RUBRIC.read_text(encoding="utf-8")

    async def _judge(request: str, answer: str, evidence: list[str]) -> FabricationVerdict | None:
        body = (
            f"Request:\n{request}\n\nAnswer:\n{answer}\n\nTool results:\n"
            + ("\n".join(evidence) or "(none)")
        )
        resp = await gateway.generate(
            role=VALIDATOR,
            system=rubric,
            contents=body,
            workspace_id=workspace_id,
            run_id=run_id,
            response_schema=FabricationVerdict,
            settings=pinned,
        )
        return parse_structured(resp, FabricationVerdict)

    return _judge


def cohen_kappa(a: list[str], b: list[str]) -> float:
    """Agreement between two raters beyond chance. 1 is perfect, 0 is chance, below 0 is worse
    than chance. §21.4 asks for it on ~50 hand labels before the judge gates anything."""
    assert len(a) == len(b) and a, "need two equal-length, non-empty label lists"
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    labels = set(a) | set(b)
    expected = sum((a.count(k) / n) * (b.count(k) / n) for k in labels)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)
