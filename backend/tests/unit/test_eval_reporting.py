"""Phase 8 D2-D4: the harness's scoring and reporting pieces that need no model: the fabrication
judge wiring, pass@1 / pass^n, the concurrency cap, the config_matrix relative gate and the
report round trip through `compare`."""

import json
import uuid
from decimal import Decimal

import pytest
from relay_eval.cases import EvalCase, Expectations
from relay_eval.cli import _compare, relative_gate
from relay_eval.harness import SuiteReport, effective_concurrency
from relay_eval.judge import FabricationVerdict, cohen_kappa
from relay_eval.report import write_report
from relay_eval.scoring import CaseResult, score_case

from relay_core.db.models.runs import AgentRun

pytestmark = pytest.mark.asyncio


def _case(**expectations) -> EvalCase:
    return EvalCase(
        key="k",
        suite="capability_detection",
        message="m",
        expectations=Expectations(**expectations),
    )


def _run() -> AgentRun:
    return AgentRun(
        id=uuid.uuid4(),
        status="completed",
        route="task",
        cost_usd=Decimal(0),
        missing_capabilities=None,
    )


async def _score(case: EvalCase, judge, *, gate: bool = True) -> CaseResult:
    return await score_case(
        case,
        _run(),
        [],
        None,
        0.0,
        final_answer="Globex renews on 2026-10-01.",
        judge=judge,
        fabrication_gate=gate,
    )


async def test_a_fabricated_verdict_fails_a_flagged_case() -> None:
    calls = []

    async def judge(request, answer, evidence):
        calls.append(answer)
        return FabricationVerdict(verdict="fabricated", claims=["renews on 2026-10-01"])

    result = await _score(_case(fabrication_check=True), judge)
    assert not result.passed and "fabrication" in result.reasons[0]
    # Gate off (uncalibrated): the finding is reported, not failing.
    result = await _score(_case(fabrication_check=True), judge, gate=False)
    assert result.passed and result.notes


async def test_a_case_without_the_flag_never_calls_the_judge() -> None:
    async def judge(*_):
        raise AssertionError("judge called")

    assert (await _score(_case(), judge)).passed


async def test_a_failing_judge_leaves_the_case_unscored_not_failed() -> None:
    async def judge(*_):
        raise RuntimeError("judge down")

    result = await _score(_case(fabrication_check=True), judge)
    assert result.passed and result.unscored


def test_pass_at_1_and_pass_hat_3() -> None:
    report = SuiteReport(
        suite="s", results=[CaseResult(key="a", passed=p) for p in (True, False, True)]
    )
    assert report.pass_at_1() == {"a": 1.0}
    assert report.pass_hat_n() == {"a": 0.0}
    assert report.pass_rate == pytest.approx(2 / 3)


def test_dependent_or_mock_backed_suites_run_sequentially() -> None:
    plain = EvalCase(key="a", suite="s", message="m", expectations=Expectations())
    dependent = plain.model_copy(update={"key": "b", "depends_on_case": "a"})
    full = plain.model_copy(update={"key": "c", "connector_profile": "full"})
    assert effective_concurrency([plain], 4) == 4
    assert effective_concurrency([plain, dependent], 4) == 1
    assert effective_concurrency([plain, full], 4) == 1


def test_relative_gate_arithmetic() -> None:
    assert relative_gate({"full": 0.5}, None) == (True, [])  # first run records, passes
    assert relative_gate({"none": 0.76}, {"none": 0.8})[0]  # within 5 points
    ok, why = relative_gate({"none": 0.7, "full": 0.2}, {"none": 0.8, "full": 0.9, "x": 1.0})
    assert not ok and len(why) == 2
    assert relative_gate({"new": 0.0}, {"full": 1.0})[0]  # no baseline for this profile yet


def test_cohen_kappa() -> None:
    assert cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    assert cohen_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == 0.0


def test_the_report_round_trips_through_compare(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("relay_eval.report._REPORTS_DIR", tmp_path)
    report = SuiteReport(
        suite="s", results=[CaseResult(key="a", passed=p, profile="none") for p in (True, False)]
    )
    path = write_report(report)
    data = json.loads(path.read_text())
    assert data["cases"]["a"] == {"attempts": 2, "pass_at_1": 1.0, "pass_hat_n": 0.0}
    _compare(path, path)
    assert "pass@1=1 pass^2=0" in capsys.readouterr().out
