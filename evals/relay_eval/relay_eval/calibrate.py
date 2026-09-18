"""`relay-eval calibrate`: how far to trust the fabrication judge (section 21.4, Phase 8 D2).

Runs the judge over hand-labelled examples and reports Cohen's kappa against the labels. The
labels live in `evals/judges/fabrication_labels.yaml`, one entry per answer:

    - request: "..."
      answer: "..."
      evidence: ["tool_name: {...}", ...]
      label: supported | fabricated

Label answers from a real `relay-eval run --all` pass; ~50 is what section 21.4 asks for. As a
rule of thumb, turn `--fabrication-gate` on only above kappa 0.6 ("substantial agreement").
"""

import uuid
from pathlib import Path

import yaml
from redis.asyncio import Redis
from relay_core.config import get_settings
from relay_core.llm.gateway import build_llm_gateway
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from relay_eval.judge import build_judge, cohen_kappa

_DEFAULT_LABELS = Path(__file__).resolve().parents[2] / "judges" / "fabrication_labels.yaml"


async def run_calibration(path: Path | None) -> None:
    labels = yaml.safe_load((path or _DEFAULT_LABELS).read_text()) or []
    if not labels:
        print(f"No labels in {path or _DEFAULT_LABELS}; label a real run's answers first.")
        return

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url)
    human, judged, skipped = [], [], 0
    try:
        async with async_sessionmaker(engine)() as session:
            # Calibration spend is attributed to a throwaway workspace id: it belongs to no run.
            judge = build_judge(
                build_llm_gateway(session, redis, settings), settings, uuid.UUID(int=0), None
            )
            for item in labels:
                verdict = await judge(item["request"], item["answer"], item.get("evidence", []))
                if verdict is None:
                    skipped += 1
                    continue
                human.append(item["label"])
                judged.append(verdict.verdict)
    finally:
        await redis.aclose()
        await engine.dispose()

    agree = sum(h == j for h, j in zip(human, judged, strict=True))
    print(f"labels: {len(labels)}, judged: {len(judged)}, no verdict: {skipped}")
    print(f"raw agreement: {agree}/{len(judged)}")
    print(f"Cohen's kappa: {cohen_kappa(human, judged):.2f}")
