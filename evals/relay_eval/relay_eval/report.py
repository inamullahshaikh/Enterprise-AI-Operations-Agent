"""Console + JSON reporting for a suite run (docs/system-design.md section 21.5). No
`eval_runs`/`eval_results` DB tables yet (docs/adr/0009) — a JSON file under `evals/reports/`
is what Phase 3's "done when" means by "the first eval report exists". Trend charts, per-case
diffs, and an Evals page wait for enough of these to be worth building a UI over (Phase 8).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from relay_eval.harness import SuiteReport

_REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def print_report(report: SuiteReport) -> None:
    print(
        f"\n{report.suite}: {report.passed}/{len(report.results)} passed ({report.pass_rate:.0%})"
    )
    for result in report.results:
        status = "PASS" if result.passed else "FAIL"
        detail = f" — {'; '.join(result.reasons)}" if result.reasons else ""
        extra = "; ".join(result.notes + result.unscored)
        print(f"  [{status}] {result.key}{detail}" + (f" ({extra})" if extra else ""))
    attempts = {len(rs) for rs in report.by_case().values()}
    if attempts != {1}:
        at1, hat = report.pass_at_1(), report.pass_hat_n()
        print(
            f"  pass@1 {sum(at1.values()) / len(at1):.0%}, "
            f"pass^{max(attempts)} {sum(hat.values()) / len(hat):.0%}"
        )


def write_report(report: SuiteReport) -> Path:
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _REPORTS_DIR / f"{report.suite}_{timestamp}.json"
    payload = {
        "suite": report.suite,
        "pass_rate": report.pass_rate,
        "violations": report.violations,
        "cost_usd": str(sum((r.cost_usd for r in report.results), start=0)),
        "profile_pass_rates": report.profile_pass_rates(),
        "cases": {
            key: {
                "attempts": len(rs),
                "pass_at_1": report.pass_at_1()[key],
                "pass_hat_n": report.pass_hat_n()[key],
            }
            for key, rs in report.by_case().items()
        },
        "results": [
            {
                "key": r.key,
                "passed": r.passed,
                "reasons": r.reasons,
                "notes": r.notes,
                "unscored": r.unscored,
                "profile": r.profile,
                "cost_usd": str(r.cost_usd),
                "latency_s": r.latency_s,
                "violations": r.violations,
            }
            for r in report.results
        ],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path
