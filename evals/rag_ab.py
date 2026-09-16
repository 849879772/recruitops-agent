from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


Strategy: TypeAlias = Literal["baseline", "rag"]
STRATEGIES: tuple[Strategy, Strategy] = ("baseline", "rag")
DEFAULT_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "rag_onboarding_cases.json"


def estimate_tokens(text: str) -> int:
    """Estimate tokens with a fixed, model-independent four-character heuristic."""

    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OnboardingJob(EvalModel):
    job_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    detail_url: str = Field(min_length=1)


class GroundTruth(EvalModel):
    """Authoritative expected outcome, written independently of executor output."""

    status: str = Field(min_length=1)
    jobs: list[OnboardingJob] = Field(min_length=0)

    @model_validator(mode="after")
    def validate_unique_job_ids(self) -> "GroundTruth":
        job_ids = [job.job_id for job in self.jobs]
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("ground truth job IDs must be unique")
        return self


class RetrievalEvidence(EvalModel):
    source_ref: str = Field(min_length=1)
    content: str = Field(min_length=1)


class SyntheticExecution(EvalModel):
    """A deterministic offline executor response stored in the fixture."""

    classification: Literal["synthetic"] = "synthetic"
    status: str = Field(min_length=1)
    jobs: list[OnboardingJob] = Field(default_factory=list)
    steps: int = Field(ge=0)
    manual_interventions: int = Field(default=0, ge=0)
    input_text: str = ""
    output_text: str = ""
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    estimated_output_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def fill_token_estimates(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if data.get("estimated_input_tokens") is None:
            data["estimated_input_tokens"] = estimate_tokens(str(data.get("input_text", "")))
        if data.get("estimated_output_tokens") is None:
            data["estimated_output_tokens"] = estimate_tokens(str(data.get("output_text", "")))
        return data


class RagOnboardingCase(EvalModel):
    case_id: str = Field(min_length=1)
    company: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    frozen_at: str = Field(min_length=1)
    observations: dict[str, object] = Field(default_factory=dict)
    retrieval_evidence: list[RetrievalEvidence] = Field(default_factory=list)
    ground_truth: GroundTruth
    synthetic_runs: dict[str, SyntheticExecution]

    @model_validator(mode="after")
    def validate_strategies(self) -> "RagOnboardingCase":
        if set(self.synthetic_runs) != set(STRATEGIES):
            raise ValueError("each case needs exactly baseline and rag synthetic runs")
        return self


class RagOnboardingFixture(EvalModel):
    fixture_id: str = "rag-website-onboarding-v1"
    version: int = Field(default=1, ge=1)
    source_type: Literal["fixture"] = "fixture"
    result_type: Literal["synthetic"] = "synthetic"
    frozen_at: str = Field(min_length=1)
    description: str = Field(min_length=1)
    cases: list[RagOnboardingCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cases(self) -> "RagOnboardingFixture":
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("fixture case IDs must be unique")
        if any(case.frozen_at != self.frozen_at for case in self.cases):
            raise ValueError("fixture and case frozen_at values must match")
        return self


class OnboardingExecution(EvalModel):
    """Normalized output returned by a baseline or RAG executor."""

    status: str = Field(min_length=1)
    jobs: list[OnboardingJob] = Field(default_factory=list)
    steps: int = Field(ge=0)
    manual_interventions: int = Field(default=0, ge=0)
    input_text: str = ""
    output_text: str = ""
    estimated_input_tokens: int | None = Field(default=None, ge=0)
    estimated_output_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def fill_token_estimates(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if data.get("estimated_input_tokens") is None:
            data["estimated_input_tokens"] = estimate_tokens(str(data.get("input_text", "")))
        if data.get("estimated_output_tokens") is None:
            data["estimated_output_tokens"] = estimate_tokens(str(data.get("output_text", "")))
        return data


class OnboardingExecutor(Protocol):
    def execute(
        self, case: RagOnboardingCase, strategy: Strategy
    ) -> OnboardingExecution | Mapping[str, object]: ...


Executor: TypeAlias = Callable[
    [RagOnboardingCase, Strategy], OnboardingExecution | Mapping[str, object]
]


class FixtureExecutor:
    """Run the frozen, synthetic responses without a browser, model, or network."""

    def execute(
        self, case: RagOnboardingCase, strategy: Strategy
    ) -> SyntheticExecution:
        return case.synthetic_runs[strategy]


class CaseEvaluation(EvalModel):
    case_id: str
    company: str
    strategy: Strategy
    success: bool
    expected_status: str
    actual_status: str
    expected_jobs: list[OnboardingJob]
    actual_jobs: list[OnboardingJob]
    mismatch_reasons: list[str] = Field(default_factory=list)
    steps: int = Field(ge=0)
    manual_interventions: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)


class StrategySummary(EvalModel):
    strategy: Strategy
    cases: int = Field(ge=0)
    successful_cases: int = Field(ge=0)
    success_rate: float = Field(ge=0.0, le=1.0)
    total_steps: int = Field(ge=0)
    mean_steps: float = Field(ge=0.0)
    manual_interventions: int = Field(ge=0)
    manual_intervention_cases: int = Field(ge=0)
    manual_intervention_rate: float = Field(ge=0.0, le=1.0)
    estimated_input_tokens: int = Field(ge=0)
    mean_estimated_input_tokens: float = Field(ge=0.0)
    estimated_output_tokens: int = Field(ge=0)
    mean_estimated_output_tokens: float = Field(ge=0.0)
    results: list[CaseEvaluation] = Field(default_factory=list)


class RagABReport(EvalModel):
    report_version: int = Field(default=1, ge=1)
    evaluation: str = "website_onboarding_rag_ab"
    source_type: Literal["fixture"] = "fixture"
    result_type: Literal["synthetic"] = "synthetic"
    synthetic: bool = True
    fixture_id: str = Field(min_length=1)
    frozen_at: str = Field(min_length=1)
    cases: int = Field(ge=0)
    baseline: StrategySummary
    rag: StrategySummary
    rag_minus_baseline: dict[str, float]


def load_rag_onboarding_fixture(path: Path | None = None) -> RagOnboardingFixture:
    fixture_path = path or DEFAULT_FIXTURE_PATH
    return RagOnboardingFixture.model_validate_json(fixture_path.read_text(encoding="utf-8"))


def _canonical_jobs(jobs: Iterable[OnboardingJob]) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted((job.job_id, job.title, job.detail_url) for job in jobs))


def _invoke_executor(
    executor: OnboardingExecutor | Executor,
    case: RagOnboardingCase,
    strategy: Strategy,
) -> OnboardingExecution:
    if hasattr(executor, "execute"):
        raw = executor.execute(case, strategy)  # type: ignore[union-attr]
    elif hasattr(executor, "run"):
        raw = executor.run(case, strategy)  # type: ignore[union-attr]
    elif callable(executor):
        raw = executor(case, strategy)
    else:
        raise TypeError("executor must be callable or expose execute(case, strategy)")
    if isinstance(raw, OnboardingExecution):
        return raw
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="python", exclude={"classification"})
    return OnboardingExecution.model_validate(raw)


def _evaluate_case(
    case: RagOnboardingCase,
    strategy: Strategy,
    execution: OnboardingExecution,
) -> CaseEvaluation:
    expected = case.ground_truth
    mismatch_reasons: list[str] = []
    if execution.status != expected.status:
        mismatch_reasons.append("status_mismatch")
    if _canonical_jobs(execution.jobs) != _canonical_jobs(expected.jobs):
        mismatch_reasons.append("jobs_mismatch")
    return CaseEvaluation(
        case_id=case.case_id,
        company=case.company,
        strategy=strategy,
        success=not mismatch_reasons,
        expected_status=expected.status,
        actual_status=execution.status,
        expected_jobs=expected.jobs,
        actual_jobs=execution.jobs,
        mismatch_reasons=mismatch_reasons,
        steps=execution.steps,
        manual_interventions=execution.manual_interventions,
        estimated_input_tokens=execution.estimated_input_tokens or 0,
        estimated_output_tokens=execution.estimated_output_tokens or 0,
    )


def _summarize(strategy: Strategy, results: list[CaseEvaluation]) -> StrategySummary:
    count = len(results)
    successful = sum(result.success for result in results)
    total_steps = sum(result.steps for result in results)
    manual_interventions = sum(result.manual_interventions for result in results)
    manual_cases = sum(result.manual_interventions > 0 for result in results)
    input_tokens = sum(result.estimated_input_tokens for result in results)
    output_tokens = sum(result.estimated_output_tokens for result in results)
    return StrategySummary(
        strategy=strategy,
        cases=count,
        successful_cases=successful,
        success_rate=successful / count if count else 0.0,
        total_steps=total_steps,
        mean_steps=total_steps / count if count else 0.0,
        manual_interventions=manual_interventions,
        manual_intervention_cases=manual_cases,
        manual_intervention_rate=manual_cases / count if count else 0.0,
        estimated_input_tokens=input_tokens,
        mean_estimated_input_tokens=input_tokens / count if count else 0.0,
        estimated_output_tokens=output_tokens,
        mean_estimated_output_tokens=output_tokens / count if count else 0.0,
        results=results,
    )


def run_rag_ab(
    cases_or_fixture: Iterable[RagOnboardingCase] | RagOnboardingFixture,
    executor: OnboardingExecutor | Executor | None = None,
    *,
    fixture_id: str | None = None,
    frozen_at: str | None = None,
) -> RagABReport:
    """Run both strategies over exactly the same cases with an injectable executor."""

    if isinstance(cases_or_fixture, RagOnboardingFixture):
        cases = list(cases_or_fixture.cases)
        fixture_id = cases_or_fixture.fixture_id
        frozen_at = cases_or_fixture.frozen_at
    else:
        cases = list(cases_or_fixture)
        fixture_id = fixture_id or "injected-fixture"
        frozen_at = frozen_at or "unspecified"

    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("case IDs must be unique for a paired A/B evaluation")

    runner = executor or FixtureExecutor()
    results: dict[Strategy, list[CaseEvaluation]] = {"baseline": [], "rag": []}
    for strategy in STRATEGIES:
        for case in cases:
            execution = _invoke_executor(runner, case, strategy)
            results[strategy].append(_evaluate_case(case, strategy, execution))

    baseline = _summarize("baseline", results["baseline"])
    rag = _summarize("rag", results["rag"])
    return RagABReport(
        fixture_id=fixture_id,
        frozen_at=frozen_at,
        cases=len(cases),
        baseline=baseline,
        rag=rag,
        rag_minus_baseline={
            "success_rate": rag.success_rate - baseline.success_rate,
            "mean_steps": rag.mean_steps - baseline.mean_steps,
            "manual_intervention_rate": (
                rag.manual_intervention_rate - baseline.manual_intervention_rate
            ),
            "mean_estimated_input_tokens": (
                rag.mean_estimated_input_tokens - baseline.mean_estimated_input_tokens
            ),
            "mean_estimated_output_tokens": (
                rag.mean_estimated_output_tokens - baseline.mean_estimated_output_tokens
            ),
        },
    )


def run_frozen_rag_ab(
    path: Path | None = None,
    executor: OnboardingExecutor | Executor | None = None,
) -> RagABReport:
    fixture = load_rag_onboarding_fixture(path)
    return run_rag_ab(fixture, executor=executor)


def render_json_report(report: RagABReport) -> str:
    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline website-onboarding RAG A/B eval.")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE_PATH,
        help="Path to a frozen rag onboarding fixture JSON file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the JSON report; the report is also printed to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_frozen_rag_ab(args.fixture)
    serialized = render_json_report(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


__all__ = [
    "CaseEvaluation",
    "DEFAULT_FIXTURE_PATH",
    "Executor",
    "FixtureExecutor",
    "GroundTruth",
    "OnboardingExecution",
    "OnboardingExecutor",
    "OnboardingJob",
    "RagABReport",
    "RagOnboardingCase",
    "RagOnboardingFixture",
    "RetrievalEvidence",
    "STRATEGIES",
    "StrategySummary",
    "estimate_tokens",
    "load_rag_onboarding_fixture",
    "main",
    "render_json_report",
    "run_frozen_rag_ab",
    "run_rag_ab",
]


if __name__ == "__main__":
    raise SystemExit(main())
