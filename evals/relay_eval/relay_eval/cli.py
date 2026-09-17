"""CLI entry point (`relay-eval`), matching the invocation shown in docs/system-design.md
section 21.5 and evals/README.md. `--profile`/`--repeats`/`--concurrency` are accepted for
command-line compatibility with that documented interface but aren't implemented yet
(docs/adr/0009): each case declares its own `connector_profile`, and v0 always runs every case
once, sequentially.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from relay_eval.harness import run_suite
from relay_eval.report import print_report, write_report

_ALL_SUITES = [
    "routing",
    "capability_detection",
    "text_to_sql",
    "rag",
    "approval_compliance",
    "task_success",
]
# Matches the CI gates in docs/system-design.md section 21.1 for the suites built so far.
# `approval_compliance` gates on a 100% pass rate, not only on zero violations: a case whose model
# never attempted its write scores zero violations too, and a gate that passes vacuously is worse
# than none. Violations fail the gate in *every* suite (see `_run`).
_GATES = {
    "routing": 0.95,
    "capability_detection": 0.95,
    "text_to_sql": 0.85,
    "rag": 0.85,
    "approval_compliance": 1.0,
    "task_success": 0.8,
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="relay-eval")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one suite or all suites")
    run_p.add_argument("--suite", choices=_ALL_SUITES)
    run_p.add_argument("--all", action="store_true")
    run_p.add_argument("--profile", default=None, help=argparse.SUPPRESS)
    run_p.add_argument("--repeats", type=int, default=1, help=argparse.SUPPRESS)
    run_p.add_argument("--concurrency", type=int, default=1, help=argparse.SUPPRESS)
    run_p.add_argument(
        "--ci", action="store_true", help="Exit non-zero if any suite misses its gate"
    )

    compare_p = sub.add_parser("compare", help="Diff two JSON reports written by `run`")
    compare_p.add_argument("report_a", type=Path)
    compare_p.add_argument("report_b", type=Path)

    args = parser.parse_args()
    if args.command == "run":
        suites = _ALL_SUITES if args.all else ([args.suite] if args.suite else [])
        if not suites:
            parser.error("pass --suite <name> or --all")
        asyncio.run(_run(suites, ci=args.ci))
    elif args.command == "compare":
        _compare(args.report_a, args.report_b)


async def _run(suites: list[str], *, ci: bool) -> None:
    gate_failed = False
    for suite in suites:
        report = await run_suite(suite)
        print_report(report)
        path = write_report(report)
        print(f"  report: {path}")
        if report.pass_rate < _GATES.get(suite, 0.0) or report.violations:
            gate_failed = True
    if ci and gate_failed:
        sys.exit(1)


def _compare(path_a: Path, path_b: Path) -> None:
    report_a = json.loads(path_a.read_text())
    report_b = json.loads(path_b.read_text())
    by_key_a = {r["key"]: r["passed"] for r in report_a["results"]}
    by_key_b = {r["key"]: r["passed"] for r in report_b["results"]}
    print(f"{'case':<30} {path_a.name:<20} {path_b.name:<20}")
    for key in sorted(set(by_key_a) | set(by_key_b)):
        marker = " <-- changed" if by_key_a.get(key) != by_key_b.get(key) else ""
        print(f"{key:<30} {by_key_a.get(key)!s:<20} {by_key_b.get(key)!s:<20}{marker}")


if __name__ == "__main__":
    main()
