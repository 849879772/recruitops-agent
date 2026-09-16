from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .metrics import WebsiteEvalResult, WebsiteEvalSummary, summarize_websites


class BlindEvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WebsiteBlindCase(BlindEvalModel):
    case_id: str = Field(min_length=1)
    company: str = Field(min_length=1)
    crawler: str = Field(min_length=1)
    source_url: HttpUrl
    expected_jobs: int = Field(ge=1)
    expected_detail_jobs: int = Field(ge=0)
    expected_status: str
    frozen_at: str


class WebsiteBlindFixture(BlindEvalModel):
    version: int = 1
    source_snapshot: str
    selection: str
    development_exclusion: str
    cases: list[WebsiteBlindCase] = Field(min_length=30)

    @model_validator(mode="after")
    def validate_unique_cases(self) -> "WebsiteBlindFixture":
        ids = [case.case_id for case in self.cases]
        companies = [case.company.casefold() for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("blind case IDs must be unique")
        if len(companies) != len(set(companies)):
            raise ValueError("blind companies must be unique")
        return self


class WebsiteBlindObservation(BlindEvalModel):
    """Normalized output for one offline or explicitly live blind-test case."""

    case_id: str | None = None
    company: str | None = None
    connected: bool = False
    found_jobs: int = Field(default=0, ge=0)
    found_detail_jobs: int = Field(default=0, ge=0)
    correct_titles: int = Field(default=0, ge=0)
    complete_jd: int = Field(default=0, ge=0)
    production_eligible_jobs: int | None = Field(default=None, ge=0)
    production_eligible_complete_jd: int | None = Field(default=None, ge=0)
    hydration_attempted: int = Field(default=0, ge=0)
    hydration_succeeded: int = Field(default=0, ge=0)
    erroneous_writes: int = Field(default=0, ge=0)
    manual_interventions: int = Field(default=0, ge=0)
    steps: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    token_cost: float = Field(default=0.0, ge=0.0)
    cost_usd: float | None = Field(default=None, ge=0.0)
    failure_categories: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_identity(self) -> "WebsiteBlindObservation":
        if not (self.case_id or self.company):
            raise ValueError("blind observation needs case_id or company")
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
        if self.hydration_succeeded > self.hydration_attempted:
            raise ValueError("hydration_succeeded cannot exceed hydration_attempted")
        return self


class WebsiteBlindManifestStats(BlindEvalModel):
    cases: int = Field(ge=0)
    expected_jobs: int = Field(ge=0)
    expected_detail_jobs: int = Field(ge=0)
    companies: list[str] = Field(default_factory=list)
    by_crawler: dict[str, int] = Field(default_factory=dict)


LiveBlindRunner = Callable[[WebsiteBlindCase], Mapping[str, object]]


def load_website_blind_fixture(path: Path | None = None) -> WebsiteBlindFixture:
    fixture_path = path or Path(__file__).parent / "fixtures" / "website_blind_30.json"
    return WebsiteBlindFixture.model_validate_json(fixture_path.read_text(encoding="utf-8"))


def website_blind_manifest_stats(
    fixture: WebsiteBlindFixture | None = None,
) -> WebsiteBlindManifestStats:
    loaded = fixture or load_website_blind_fixture()
    crawlers = Counter(case.crawler for case in loaded.cases)
    return WebsiteBlindManifestStats(
        cases=len(loaded.cases),
        expected_jobs=sum(case.expected_jobs for case in loaded.cases),
        expected_detail_jobs=sum(case.expected_detail_jobs for case in loaded.cases),
        companies=[case.company for case in loaded.cases],
        by_crawler=dict(sorted(crawlers.items())),
    )


def _observation_map(
    observations: Iterable[WebsiteBlindObservation | Mapping[str, object]],
) -> dict[str, WebsiteBlindObservation]:
    by_key: dict[str, WebsiteBlindObservation] = {}
    for value in observations:
        observation = (
            value
            if isinstance(value, WebsiteBlindObservation)
            else WebsiteBlindObservation.model_validate(value)
        )
        normalized_key = (
            f"id:{observation.case_id}"
            if observation.case_id
            else f"company:{observation.company.casefold()}"
        )
        if normalized_key in by_key:
            raise ValueError(f"duplicate blind observation: {normalized_key}")
        by_key[normalized_key] = observation
    return by_key


def _failure_categories(
    case: WebsiteBlindCase,
    observation: WebsiteBlindObservation,
) -> list[str]:
    categories = list(dict.fromkeys(item for item in observation.failure_categories if item))
    found_detail_jobs = observation.found_detail_jobs or observation.complete_jd
    production_evaluated = observation.production_eligible_jobs is not None
    if not observation.connected:
        categories.append("not_connected")
    if observation.found_jobs < case.expected_jobs:
        categories.append("job_recall_miss")
    if not production_evaluated and found_detail_jobs < case.expected_detail_jobs:
        categories.append("detail_recall_miss")
    if observation.correct_titles < observation.found_jobs:
        categories.append("title_mismatch")
    if (
        production_evaluated
        and (observation.production_eligible_complete_jd or 0)
        < (observation.production_eligible_jobs or 0)
    ):
        categories.append("production_jd_incomplete")
    elif not production_evaluated and observation.complete_jd < observation.found_jobs:
        categories.append("jd_incomplete")
    if observation.erroneous_writes:
        categories.append("erroneous_write")
    if observation.manual_interventions:
        categories.append("manual_intervention")
    return list(dict.fromkeys(categories))


def evaluate_website_blind(
    fixture: WebsiteBlindFixture | None = None,
    observations: Iterable[WebsiteBlindObservation | Mapping[str, object]] = (),
    *,
    mode: str = "offline",
    synthetic: bool = True,
) -> WebsiteEvalSummary:
    """Score observations against the frozen 30-company manifest.

    Observations may be keyed by ``case_id`` or by the frozen company name. A
    missing observation is treated as a failed case, which prevents an
    incomplete live run from looking successful.
    """

    loaded = fixture or load_website_blind_fixture()
    by_key = _observation_map(observations)
    results: list[WebsiteEvalResult] = []
    consumed: set[str] = set()
    for case in loaded.cases:
        id_key = f"id:{case.case_id}"
        company_key = f"company:{case.company.casefold()}"
        observation = by_key.get(id_key) or by_key.get(company_key)
        if observation is None:
            observation = WebsiteBlindObservation(case_id=case.case_id, company=case.company)
        else:
            consumed.add(id_key if id_key in by_key else company_key)
        found_detail_jobs = observation.found_detail_jobs or observation.complete_jd
        results.append(
            WebsiteEvalResult(
                company=case.company,
                connected=observation.connected,
                expected_jobs=case.expected_jobs,
                found_jobs=observation.found_jobs,
                correct_titles=observation.correct_titles,
                complete_jd=observation.complete_jd,
                production_eligible_jobs=observation.production_eligible_jobs,
                production_eligible_complete_jd=(
                    observation.production_eligible_complete_jd
                ),
                expected_detail_jobs=case.expected_detail_jobs,
                erroneous_writes=observation.erroneous_writes,
                manual_interventions=observation.manual_interventions,
                steps=observation.steps,
                token_cost=observation.token_cost,
                cost_usd=observation.cost_usd,
                found_detail_jobs=found_detail_jobs,
                input_tokens=observation.input_tokens,
                output_tokens=observation.output_tokens,
                latency_ms=observation.latency_ms,
                failure_categories=_failure_categories(case, observation),
            )
        )
    unknown = set(by_key) - consumed
    if unknown:
        raise ValueError(f"observations contain unknown blind cases: {sorted(unknown)}")
    summary = summarize_websites(results)
    return summary.model_copy(update={"mode": mode, "synthetic": synthetic})


def run_website_blind_live(
    fixture: WebsiteBlindFixture | None,
    runner: LiveBlindRunner,
) -> WebsiteEvalSummary:
    loaded = fixture or load_website_blind_fixture()
    observations = [runner(case) for case in loaded.cases]
    return evaluate_website_blind(loaded, observations, mode="live", synthetic=False)


__all__ = [
    "LiveBlindRunner",
    "WebsiteBlindCase",
    "WebsiteBlindFixture",
    "WebsiteBlindManifestStats",
    "WebsiteBlindObservation",
    "evaluate_website_blind",
    "load_website_blind_fixture",
    "run_website_blind_live",
    "website_blind_manifest_stats",
]
