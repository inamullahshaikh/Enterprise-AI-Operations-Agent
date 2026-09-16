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
        print(f"  [{status}] {result.key}{detail}")


def write_report(report: SuiteReport) -> Path:
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = _REPORTS_DIR / f"{report.suite}_{timestamp}.json"
    payload = {
        "suite": report.suite,
        "pass_rate": report.pass_rate,
        "results": [
            {
                "key": r.key,
                "passed": r.passed,
                "reasons": r.reasons,
                "cost_usd": str(r.cost_usd),
                "latency_s": r.latency_s,
            }
            for r in report.results
        ],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path
