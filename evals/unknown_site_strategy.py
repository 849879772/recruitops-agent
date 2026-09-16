from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "unknown_site_strategy_cases.json"
STRATEGIES = ("platform_adapter", "generic_extraction", "verified_manual_candidate")


class EvalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StrategySpec(EvalModel):
    name: str
    crawler: str
    url: str


class UnknownSiteCase(EvalModel):
    case_id: str
    company: str
    source_url: str
    expected_titles: list[str] = Field(min_length=1)
    strategies: list[StrategySpec] = Field(min_length=3, max_length=3)


class UnknownSiteFixture(EvalModel):
    fixture_id: str
    frozen_at: str
    description: str
    cases: list[UnknownSiteCase] = Field(min_length=2)


class StrategyObservation(EvalModel):
    case_id: str
    company: str
    strategy: str
    crawler: str
    url: str
    job_count: int = Field(ge=0)
    correct_titles: int = Field(ge=0)
    unexpected_titles: list[str] = Field(default_factory=list)
    title_recall: float = Field(ge=0.0, le=1.0)
    title_precision: float = Field(ge=0.0, le=1.0)
    elapsed_ms: int = Field(ge=0)
    safe_to_connect: bool = False
    error: str | None = None


class StrategySummary(EvalModel):
    strategy: str
    cases: int = Field(ge=0)
    safe_connections: int = Field(ge=0)
    mean_title_recall: float = Field(ge=0.0, le=1.0)
    mean_title_precision: float = Field(ge=0.0, le=1.0)
    elapsed_ms: int = Field(ge=0)


class UnknownSiteStrategyReport(EvalModel):
    report_version: int = 1
    evaluation: str = "unknown_site_strategy_live"
    result_type: str = "live"
    synthetic: bool = False
    fixture_id: str
    frozen_at: str
    observations: list[StrategyObservation]
    summaries: list[StrategySummary]
    conclusion: str
    boundary: str


ProbeRunner = Callable[[UnknownSiteCase, StrategySpec], tuple[list[str], int, str | None]]


def load_fixture(path: Path = DEFAULT_FIXTURE_PATH) -> UnknownSiteFixture:
    fixture = UnknownSiteFixture.model_validate_json(path.read_text(encoding="utf-8"))
    for case in fixture.cases:
        names = tuple(strategy.name for strategy in case.strategies)
        if names != STRATEGIES:
            raise ValueError(f"{case.case_id} must define strategies in canonical order")
    return fixture


def subprocess_probe(
    case: UnknownSiteCase,
    strategy: StrategySpec,
    *,
    timeout_seconds: float = 150.0,
) -> tuple[list[str], int, str | None]:
    if strategy.crawler == "manual_required":
        return [], 0, None
    command = [
        sys.executable,
        "-m",
        "scripts.run_agent_crawler",
        "--company",
        case.company,
        "--crawler",
        strategy.crawler,
        "--careers-url",
        strategy.url,
    ]
    started = perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], max(0, int((perf_counter() - started) * 1000)), "crawler_timeout"
    elapsed_ms = max(0, int((perf_counter() - started) * 1000))
    if completed.returncode != 0:
        return [], elapsed_ms, f"crawler_exit_{completed.returncode}"
    try:
        payload = json.loads(completed.stdout)
        jobs = payload.get("jobs") or []
        titles = [str(job.get("title") or "").strip() for job in jobs if isinstance(job, dict)]
    except (TypeError, ValueError, json.JSONDecodeError):
        return [], elapsed_ms, "crawler_output_invalid"
    return [title for title in titles if title], elapsed_ms, None


def run_unknown_site_strategy_eval(
    *,
    fixture: UnknownSiteFixture | None = None,
    probe_runner: ProbeRunner = subprocess_probe,
) -> UnknownSiteStrategyReport:
    loaded = fixture or load_fixture()
    observations: list[StrategyObservation] = []
    for case in loaded.cases:
        expected = set(case.expected_titles)
        for strategy in case.strategies:
            titles, elapsed_ms, error = probe_runner(case, strategy)
            observed = set(titles)
            correct = expected & observed
            unexpected = sorted(observed - expected)
            recall = len(correct) / len(expected)
            precision = len(correct) / len(observed) if observed else 0.0
            observations.append(
                StrategyObservation(
                    case_id=case.case_id,
                    company=case.company,
                    strategy=strategy.name,
                    crawler=strategy.crawler,
                    url=strategy.url,
                    job_count=len(titles),
                    correct_titles=len(correct),
                    unexpected_titles=unexpected,
                    title_recall=recall,
                    title_precision=precision,
                    elapsed_ms=elapsed_ms,
                    safe_to_connect=bool(titles) and recall == 1.0 and precision == 1.0 and error is None,
                    error=error,
                )
            )
    summaries: list[StrategySummary] = []
    for strategy in STRATEGIES:
        rows = [row for row in observations if row.strategy == strategy]
        summaries.append(
            StrategySummary(
                strategy=strategy,
                cases=len(rows),
                safe_connections=sum(row.safe_to_connect for row in rows),
                mean_title_recall=sum(row.title_recall for row in rows) / len(rows),
                mean_title_precision=sum(row.title_precision for row in rows) / len(rows),
                elapsed_ms=sum(row.elapsed_ms for row in rows),
            )
        )
    return UnknownSiteStrategyReport(
        fixture_id=loaded.fixture_id,
        frozen_at=loaded.frozen_at,
        observations=observations,
        summaries=summaries,
        conclusion=(
            "Known ATS adapters should be preferred. Generic extraction is not a safe substitute "
            "for ATS routing; a verified listing URL plus deterministic acceptance remains required "
            "for unknown self-built sites."
        ),
        boundary=(
            "This is a two-company live holdout. It measures title extraction and false positives, "
            "not automatic web search, cohort confirmation, JD hydration, or arbitrary-site coverage."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Run the live unknown-site strategy evaluation.")
    parser.add_argument("--live", action="store_true", help="Required safety flag.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("this evaluator performs network calls; pass --live")
    report = run_unknown_site_strategy_eval(fixture=load_fixture(args.fixture))
    serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
