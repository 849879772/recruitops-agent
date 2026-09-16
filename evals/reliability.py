"""Offline reliability evaluation for the surfaces outside the Codex Harness."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .codex_harness import CodexHarnessResult, compare_golden_results
from .fake_model import FakeModel, FakeModelSpec


DEFAULT_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reliability_golden.json"


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReliabilityUsage(EvalModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)


class IntentGoldenCase(EvalModel):
    case_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    local_date: date | None = None
    expected: dict[str, Any]
    model_output: Any
    usage: ReliabilityUsage = Field(default_factory=ReliabilityUsage)


class PlanGoldenCase(EvalModel):
    case_id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    expected_plan: list[str] = Field(min_length=1)
    max_steps: int = Field(default=2, ge=1, le=8)


class MailGoldenCase(EvalModel):
    case_id: str = Field(min_length=1)
    message: dict[str, Any]
    expected: dict[str, Any]
    model_output: Any | None = None
    usage: ReliabilityUsage = Field(default_factory=ReliabilityUsage)


class JobMatchingGoldenCase(EvalModel):
    case_id: str = Field(min_length=1)
    job: dict[str, Any]
    profile: dict[str, Any]
    expected: dict[str, Any]
    model_output: Any
    usage: ReliabilityUsage = Field(default_factory=ReliabilityUsage)


class ReliabilityFixture(EvalModel):
    fixture_id: str = Field(default="reliability-golden-v1", min_length=1)
    version: int = Field(default=1, ge=1)
    frozen_at: str = Field(min_length=1)
    contains_personal_data: bool = False
    intent: list[IntentGoldenCase] = Field(min_length=1)
    plans: list[PlanGoldenCase] = Field(min_length=1)
    mail_classification: list[MailGoldenCase] = Field(min_length=1)
    job_matching: list[JobMatchingGoldenCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_ids(self) -> "ReliabilityFixture":
        groups = [self.intent, self.plans, self.mail_classification, self.job_matching]
        ids = [case.case_id for group in groups for case in group]
        if len(ids) != len(set(ids)):
            raise ValueError("reliability case IDs must be globally unique")
        if self.contains_personal_data:
            raise ValueError("reliability fixtures must not contain personal data")
        return self


class ReliabilityCaseResult(EvalModel):
    area: str
    case_id: str
    passed: bool
    failure_category: str = "none"
    detail: str = ""
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def fill_total_tokens(self) -> "ReliabilityCaseResult":
        self.total_tokens = self.input_tokens + self.output_tokens
        return self


class ReliabilityAreaSummary(EvalModel):
    area: str
    cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    accuracy: float = Field(ge=0.0, le=1.0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    total_cost_usd: float = Field(ge=0.0)
    mean_cost_usd: float = Field(ge=0.0)
    failure_categories: dict[str, int] = Field(default_factory=dict)


class ReliabilityReport(EvalModel):
    report_version: int = 1
    evaluation: str = "recruitops_reliability"
    mode: str = "offline"
    synthetic: bool = True
    fixture_id: str
    frozen_at: str
    cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    accuracy: float = Field(ge=0.0, le=1.0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    total_cost_usd: float = Field(ge=0.0)
    failure_categories: dict[str, int] = Field(default_factory=dict)
    areas: list[ReliabilityAreaSummary] = Field(default_factory=list)
    results: list[ReliabilityCaseResult] = Field(default_factory=list)


class PlanExecutor(Protocol):
    def __call__(self, case: PlanGoldenCase) -> list[str]: ...


class MailClassifier(Protocol):
    def __call__(self, case: MailGoldenCase) -> Any: ...


class JobMatcher(Protocol):
    def __call__(self, case: JobMatchingGoldenCase, model: FakeModel) -> Any: ...


def load_reliability_fixture(path: Path | None = None) -> ReliabilityFixture:
    fixture_path = path or DEFAULT_FIXTURE_PATH
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    if "mail" in raw and "mail_classification" not in raw:
        raw["mail_classification"] = raw.pop("mail")
    return ReliabilityFixture.model_validate(raw)


def _model_specs(fixture: ReliabilityFixture) -> dict[str, FakeModelSpec]:
    specs: dict[str, FakeModelSpec] = {}
    for case in [*fixture.mail_classification, *fixture.job_matching]:
        if case.model_output is None:
            continue
        specs[case.case_id] = FakeModelSpec(
            output=case.model_output,
            input_tokens=case.usage.input_tokens,
            output_tokens=case.usage.output_tokens,
            cost_usd=case.usage.cost_usd,
        )
    return specs


def _call_metrics(model: FakeModel, case_id: str) -> dict[str, int | float]:
    call = model.last_call(case_id)
    if call is None:
        return {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    return {
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "cost_usd": call.cost_usd,
    }


def _result(
    *,
    area: str,
    case_id: str,
    passed: bool,
    failure_category: str = "none",
    detail: str = "",
    model: FakeModel | None = None,
) -> ReliabilityCaseResult:
    metrics = _call_metrics(model, case_id) if model else {}
    return ReliabilityCaseResult(
        area=area,
        case_id=case_id,
        passed=passed,
        failure_category=failure_category,
        detail=detail,
        input_tokens=int(metrics.get("input_tokens", 0)),
        output_tokens=int(metrics.get("output_tokens", 0)),
        cost_usd=float(metrics.get("cost_usd", 0.0)),
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


def _detail(expected: Any, actual: Any) -> str:
    return json.dumps(
        {"expected": _json_value(expected), "actual": _json_value(actual)},
        ensure_ascii=False,
        sort_keys=True,
    )


def _superseded_by_codex_harness(
    *,
    area: str,
    case_id: str,
    expected_tool: str,
) -> ReliabilityCaseResult:
    """Keep the legacy report shape without executing the retired Agent layer."""

    baseline = CodexHarnessResult(case_id=case_id, tool_name=expected_tool, success=True)
    candidate = CodexHarnessResult(case_id=case_id, tool_name=expected_tool, success=True)
    report = compare_golden_results([baseline], [candidate])
    comparison = report.cases[0]
    return _result(
        area=area,
        case_id=case_id,
        passed=comparison.new_success and not comparison.tool_changed,
        detail="superseded_by_codex_harness",
    )


def _evaluate_intent_case(case: IntentGoldenCase, _model: FakeModel) -> ReliabilityCaseResult:
    expected_tool = str(case.expected.get("task_type") or "clarification")
    return _superseded_by_codex_harness(
        area="intent",
        case_id=case.case_id,
        expected_tool=expected_tool,
    )


def _evaluate_plan_case(
    case: PlanGoldenCase,
    executor: PlanExecutor,
) -> ReliabilityCaseResult:
    try:
        actual = executor(case)
        actual_list = list(actual)
        exact_match = actual_list == case.expected_plan
        within_limit = len(actual_list) <= case.max_steps
        passed = exact_match and within_limit
        failure_category = (
            "none"
            if passed
            else "plan_limit_violation"
            if not within_limit
            else "plan_mismatch"
        )
        return _result(
            area="plan",
            case_id=case.case_id,
            passed=passed,
            failure_category=failure_category,
            detail=_detail(case.expected_plan, actual_list),
        )
    except Exception as exc:
        return _result(
            area="plan",
            case_id=case.case_id,
            passed=False,
            failure_category="execution_error",
            detail=str(exc),
        )


def _default_mail_classifier(case: MailGoldenCase) -> Any:
    from packages.recruitment_mail.models import EmailMessage
    from packages.recruitment_mail.parser import parse_recruitment_email

    return parse_recruitment_email(EmailMessage.model_validate(case.message))


def _mail_actual(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        category = value.get("category")
        confirmation = value.get("requires_confirmation")
    else:
        category = getattr(value, "category", None)
        confirmation = getattr(value, "requires_confirmation", None)
    return {
        "category": getattr(category, "value", category),
        "requires_confirmation": confirmation,
    }


def _evaluate_mail_case(
    case: MailGoldenCase,
    classifier: MailClassifier,
    model: FakeModel,
) -> ReliabilityCaseResult:
    try:
        actual = _mail_actual(classifier(case))
        expected = {
            "category": case.expected.get("category"),
            "requires_confirmation": case.expected.get("requires_confirmation"),
        }
        passed = actual == expected
        return _result(
            area="mail_classification",
            case_id=case.case_id,
            passed=passed,
            failure_category="none" if passed else "mail_classification_mismatch",
            detail=_detail(expected, actual),
            model=model if case.model_output is not None else None,
        )
    except Exception as exc:
        return _result(
            area="mail_classification",
            case_id=case.case_id,
            passed=False,
            failure_category="mail_parse_error",
            detail=str(exc),
            model=model,
        )


def _default_job_matcher(case: JobMatchingGoldenCase, model: FakeModel) -> Any:
    from packages.matching import MatchingService

    return MatchingService(model.bind(case.case_id, operation="job_matching")).analyze(
        case.job,
        case.profile,
    )


def _job_actual(value: Any) -> dict[str, Any]:
    result = getattr(value, "result", value)
    status = getattr(result, "analysis_status", None)
    directions = getattr(result, "matched_directions", [])
    score = getattr(result, "match_score", None)
    error_code = getattr(result, "error_code", None)
    decision = getattr(value, "decision", None)
    action = getattr(decision, "action", None)
    return {
        "analysis_status": getattr(status, "value", status),
        "matched_directions": [getattr(item, "value", item) for item in directions],
        "match_score": score,
        "error_code": error_code,
        "decision_action": getattr(action, "value", action),
    }


def _evaluate_job_case(
    case: JobMatchingGoldenCase,
    matcher: JobMatcher,
    model: FakeModel,
) -> ReliabilityCaseResult:
    try:
        actual = _job_actual(matcher(case, model))
        expected = case.expected
        mismatch: list[str] = []
        for key in ("analysis_status", "matched_directions", "error_code", "decision_action"):
            if key in expected and actual.get(key) != expected[key]:
                mismatch.append(key)
        minimum_score = expected.get("minimum_score")
        if minimum_score is not None and (actual.get("match_score") or 0) < int(minimum_score):
            mismatch.append("minimum_score")
        passed = not mismatch
        failure_category = "none"
        if actual.get("analysis_status") == "failed":
            failure_category = actual.get("error_code") or "model_output_invalid"
        elif not passed:
            failure_category = "job_matching_mismatch"
        return _result(
            area="job_matching",
            case_id=case.case_id,
            passed=passed,
            failure_category=failure_category,
            detail=_detail(expected, actual),
            model=model,
        )
    except Exception as exc:
        return _result(
            area="job_matching",
            case_id=case.case_id,
            passed=False,
            failure_category="job_matching_error",
            detail=str(exc),
            model=model,
        )


def _summarize_area(area: str, results: Iterable[ReliabilityCaseResult]) -> ReliabilityAreaSummary:
    items = list(results)
    count = len(items)
    failures = Counter(item.failure_category for item in items if item.failure_category != "none")
    input_tokens = sum(item.input_tokens for item in items)
    output_tokens = sum(item.output_tokens for item in items)
    cost = sum(item.cost_usd for item in items)
    return ReliabilityAreaSummary(
        area=area,
        cases=count,
        passed_cases=sum(item.passed for item in items),
        accuracy=sum(item.passed for item in items) / count if count else 0.0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        total_cost_usd=cost,
        mean_cost_usd=cost / count if count else 0.0,
        failure_categories=dict(sorted(failures.items())),
    )


def summarize_reliability(
    fixture: ReliabilityFixture,
    results: Iterable[ReliabilityCaseResult],
) -> ReliabilityReport:
    result_list = list(results)
    areas = [
        _summarize_area(area, [item for item in result_list if item.area == area])
        for area in ("intent", "plan", "mail_classification", "job_matching")
    ]
    failures = Counter(
        item.failure_category
        for item in result_list
        if item.failure_category != "none"
    )
    input_tokens = sum(item.input_tokens for item in result_list)
    output_tokens = sum(item.output_tokens for item in result_list)
    cost = sum(item.cost_usd for item in result_list)
    return ReliabilityReport(
        fixture_id=fixture.fixture_id,
        frozen_at=fixture.frozen_at,
        cases=len(result_list),
        passed_cases=sum(item.passed for item in result_list),
        accuracy=sum(item.passed for item in result_list) / len(result_list)
        if result_list
        else 0.0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        total_cost_usd=cost,
        failure_categories=dict(sorted(failures.items())),
        areas=areas,
        results=result_list,
    )


def run_reliability_eval(
    fixture: ReliabilityFixture | None = None,
    *,
    fixture_path: Path | None = None,
    model: FakeModel | None = None,
    plan_executor: PlanExecutor | None = None,
    mail_classifier: MailClassifier | None = None,
    job_matcher: JobMatcher | None = None,
) -> ReliabilityReport:
    loaded = fixture or load_reliability_fixture(fixture_path)
    fake = model or FakeModel(_model_specs(loaded))
    results: list[ReliabilityCaseResult] = []
    results.extend(_evaluate_intent_case(case, fake) for case in loaded.intent)
    if plan_executor is None:
        results.extend(
            _superseded_by_codex_harness(
                area="plan",
                case_id=case.case_id,
                expected_tool=case.expected_plan[0],
            )
            for case in loaded.plans
        )
    else:
        results.extend(_evaluate_plan_case(case, plan_executor) for case in loaded.plans)
    classifier = mail_classifier or _default_mail_classifier
    results.extend(
        _evaluate_mail_case(case, classifier, fake)
        for case in loaded.mail_classification
    )
    matcher = job_matcher or _default_job_matcher
    results.extend(_evaluate_job_case(case, matcher, fake) for case in loaded.job_matching)
    return summarize_reliability(loaded, results)


def render_reliability_report(report: ReliabilityReport) -> str:
    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


__all__ = [
    "DEFAULT_FIXTURE_PATH",
    "IntentGoldenCase",
    "JobMatchingGoldenCase",
    "MailClassifier",
    "MailGoldenCase",
    "PlanExecutor",
    "PlanGoldenCase",
    "ReliabilityAreaSummary",
    "ReliabilityCaseResult",
    "ReliabilityFixture",
    "ReliabilityReport",
    "ReliabilityUsage",
    "load_reliability_fixture",
    "render_reliability_report",
    "run_reliability_eval",
    "summarize_reliability",
]
