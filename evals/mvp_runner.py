from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .codex_harness import CodexHarnessResult, compare_golden_results


class EvalCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str
    passed: bool
    detail: str


_MVP_SCENARIOS = {
    "today_schedule",
    "today_new_jobs",
    "recommendation_explanation",
    "application_query",
    "recruitment_mail_detail",
}


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("MVP fixture must contain a cases list")
    return [
        case
        for case in cases
        if isinstance(case, dict) and case.get("scenario") in _MVP_SCENARIOS
    ]


def _contract_is_valid(case: dict[str, Any]) -> bool:
    expected = case.get("expected")
    scenario = case.get("scenario")
    if not isinstance(expected, dict):
        return False
    if scenario == "today_schedule":
        return expected.get("event_count", -1) >= 0 and expected.get("source_required") is True
    if scenario == "today_new_jobs":
        return (
            expected.get("allowed_cohort") == 2027
            and expected.get("allowed_status") == "confirmed"
            and {"internship", "unknown"}.issubset(set(expected.get("excluded_batches", [])))
        )
    if scenario == "recommendation_explanation":
        return (
            expected.get("complete_jd") is True
            and expected.get("candidate_citations_required") is True
        )
    if scenario == "application_query":
        return expected.get("outcome") == "needs_clarification" and expected.get("writes") == 0
    if scenario == "recruitment_mail_detail":
        return expected.get("redacted_record_returned") is True and expected.get("writes") == 0
    return False


def _evaluate_case(case: dict[str, Any]) -> EvalCaseResult:
    case_id = str(case.get("id", ""))
    scenario = str(case.get("scenario", ""))
    baseline = CodexHarnessResult(case_id=case_id, tool_name=scenario, success=True)
    candidate = CodexHarnessResult(
        case_id=case_id,
        tool_name=scenario,
        success=_contract_is_valid(case),
    )
    report = compare_golden_results([baseline], [candidate])
    comparison = report.cases[0]
    return EvalCaseResult(
        scenario=scenario,
        passed=comparison.new_success and not comparison.tool_changed,
        detail=(
            f"evaluation={report.evaluation}; expected_tool={comparison.old_tool}; "
            f"candidate_tool={comparison.new_tool}; candidate_success={comparison.new_success}"
        ),
    )


def run_frozen_mvp(snapshot_path: Path | None = None) -> list[EvalCaseResult]:
    """Validate the retained MVP contracts through the Codex Harness result shape."""

    path = snapshot_path or Path(__file__).parent / "fixtures" / "mvp_cases.json"
    return [_evaluate_case(case) for case in _load_cases(path)]


if __name__ == "__main__":
    output = run_frozen_mvp()
    print(json.dumps([item.model_dump(mode="json") for item in output], ensure_ascii=False, indent=2))
    raise SystemExit(0 if all(item.passed for item in output) else 1)
