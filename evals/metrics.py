from __future__ import annotations

from collections import Counter
from statistics import mean

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WebsiteEvalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str
    connected: bool
    expected_jobs: int = Field(ge=0)
    found_jobs: int = Field(ge=0)
    correct_titles: int = Field(ge=0)
    complete_jd: int = Field(ge=0)
    production_eligible_jobs: int | None = Field(default=None, ge=0)
    production_eligible_complete_jd: int | None = Field(default=None, ge=0)
    expected_detail_jobs: int = Field(default=0, ge=0)
    erroneous_writes: int = Field(default=0, ge=0)
    manual_interventions: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    token_cost: float = Field(default=0.0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    found_detail_jobs: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    failure_categories: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "WebsiteEvalResult":
        if self.correct_titles > self.found_jobs:
            raise ValueError("correct_titles cannot exceed found_jobs")
        if self.complete_jd > self.found_jobs:
            raise ValueError("complete_jd cannot exceed found_jobs")
        if self.found_detail_jobs > self.found_jobs:
            raise ValueError("found_detail_jobs cannot exceed found_jobs")
        if (
            self.production_eligible_jobs is not None
            and self.production_eligible_complete_jd is not None
            and self.production_eligible_complete_jd > self.production_eligible_jobs
        ):
            raise ValueError(
                "production_eligible_complete_jd cannot exceed production_eligible_jobs"
            )
        return self


class WebsiteEvalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: int
    integration_rate: float
    job_recall: float
    title_accuracy: float
    jd_coverage: float
    production_evaluated_cases: int = 0
    production_eligible_jobs: int = 0
    production_eligible_complete_jd: int = 0
    production_jd_coverage: float = 0.0
    erroneous_write_rate: float
    manual_intervention_rate: float
    mean_steps: float
    mean_token_cost: float
    accuracy: float = 0.0
    detail_recall: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    mean_input_tokens: float = 0.0
    mean_output_tokens: float = 0.0
    total_cost_usd: float = 0.0
    mean_latency_ms: float = 0.0
    failure_categories: dict[str, int] = Field(default_factory=dict)
    mode: str = "offline"
    synthetic: bool = True


def _cost_usd(item: WebsiteEvalResult) -> float:
    return item.cost_usd if item.cost_usd is not None else item.token_cost


def summarize_websites(results: list[WebsiteEvalResult]) -> WebsiteEvalSummary:
    if not results:
        return WebsiteEvalSummary(
            cases=0,
            integration_rate=0.0,
            job_recall=0.0,
            title_accuracy=0.0,
            jd_coverage=0.0,
            erroneous_write_rate=0.0,
            manual_intervention_rate=0.0,
            mean_steps=0.0,
            mean_token_cost=0.0,
        )
    expected = sum(item.expected_jobs for item in results)
    found = sum(item.found_jobs for item in results)
    recovered = sum(min(item.found_jobs, item.expected_jobs) for item in results)
    expected_details = sum(item.expected_detail_jobs for item in results)
    found_details = sum(
        min(item.found_detail_jobs or item.complete_jd, item.expected_detail_jobs)
        for item in results
    )
    input_tokens = sum(item.input_tokens for item in results)
    output_tokens = sum(item.output_tokens for item in results)
    failures = Counter(
        category
        for item in results
        for category in item.failure_categories
        if category
    )
    production_results = [
        item for item in results if item.production_eligible_jobs is not None
    ]
    production_eligible_jobs = sum(
        item.production_eligible_jobs or 0 for item in production_results
    )
    production_complete_jd = sum(
        item.production_eligible_complete_jd or 0 for item in production_results
    )
    accurate_cases = sum(
        item.connected
        and item.found_jobs >= item.expected_jobs
        and (item.found_detail_jobs or item.complete_jd) >= item.expected_detail_jobs
        and item.correct_titles >= item.found_jobs
        and item.complete_jd >= item.found_jobs
        and item.erroneous_writes == 0
        for item in results
    )
    return WebsiteEvalSummary(
        cases=len(results),
        integration_rate=sum(item.connected for item in results) / len(results),
        job_recall=recovered / expected if expected else 0.0,
        title_accuracy=sum(item.correct_titles for item in results) / found if found else 0.0,
        jd_coverage=sum(item.complete_jd for item in results) / found if found else 0.0,
        production_evaluated_cases=len(production_results),
        production_eligible_jobs=production_eligible_jobs,
        production_eligible_complete_jd=production_complete_jd,
        production_jd_coverage=(
            production_complete_jd / production_eligible_jobs
            if production_eligible_jobs
            else (1.0 if production_results else 0.0)
        ),
        erroneous_write_rate=(
            sum(item.erroneous_writes for item in results) / found if found else 0.0
        ),
        manual_intervention_rate=(
            sum(item.manual_interventions > 0 for item in results) / len(results)
        ),
        mean_steps=mean(item.steps for item in results),
        mean_token_cost=mean(_cost_usd(item) for item in results),
        accuracy=accurate_cases / len(results),
        detail_recall=found_details / expected_details if expected_details else 0.0,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        mean_input_tokens=input_tokens / len(results),
        mean_output_tokens=output_tokens / len(results),
        total_cost_usd=sum(_cost_usd(item) for item in results),
        mean_latency_ms=mean(item.latency_ms for item in results),
        failure_categories=dict(sorted(failures.items())),
    )


__all__ = ["WebsiteEvalResult", "WebsiteEvalSummary", "summarize_websites"]
