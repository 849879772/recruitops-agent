"""Fixture-only acceptance for OC crawler adapter candidates.

Candidates handled here are deliberately disconnected from the production
crawler registry. A passing result means "ready for human approval", never
"enabled".
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .crawler_audit import CrawlerAcceptanceInput, ObservedCrawlerJob, accept_crawler_run
from .oc_candidates import _normalized_job
from .typed import ToolModel


CandidateKind = Literal["declarative", "python"]
CandidateReviewState = Literal["fixture_failed", "awaiting_approval"]


class AdapterFixture(ToolModel):
    name: str = Field(min_length=1, max_length=100)
    input: dict[str, Any] = Field(default_factory=dict)
    expected_titles: list[str] = Field(min_length=1)


class AdapterCandidateSpec(ToolModel):
    company: str = Field(min_length=1, max_length=200)
    source_url: str = Field(min_length=8, max_length=2_048)
    kind: CandidateKind
    recipe: dict[str, Any] | None = None
    python_file: str | None = None
    fixtures: list[AdapterFixture] = Field(min_length=1)
    expected_cohort: int = Field(default=2027, ge=1, le=9_999)

    @model_validator(mode="after")
    def validate_kind_payload(self) -> "AdapterCandidateSpec":
        if self.kind == "declarative" and not self.recipe:
            raise ValueError("declarative candidates require a recipe")
        if self.kind == "declarative" and self.recipe:
            recipe_type = str(self.recipe.get("type") or "")
            if recipe_type not in {"api_campaigns", "dom", "html_list"}:
                raise ValueError(
                    "fixture acceptance supports only api_campaigns, html_list, or dom recipes"
                )
        if self.kind == "python" and not self.python_file:
            raise ValueError("python candidates require python_file")
        return self


class AdapterFixtureResult(ToolModel):
    name: str
    passed: bool
    observed_titles: list[str] = Field(default_factory=list)
    expected_titles: list[str] = Field(default_factory=list)
    accepted_count: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None


class AdapterCandidateAcceptance(ToolModel):
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    company: str
    source_url: str
    kind: CandidateKind
    state: CandidateReviewState
    runtime_enabled: Literal[False] = False
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    fixture_results: list[AdapterFixtureResult] = Field(default_factory=list)


def _candidate_id(spec: AdapterCandidateSpec, source_sha256: str | None) -> str:
    payload = spec.model_dump(mode="json", exclude={"python_file"})
    payload["source_sha256"] = source_sha256
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _resolve_python_file(value: str, candidate_root: Path) -> tuple[Path, str]:
    root = candidate_root.expanduser().resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if path.suffix.casefold() != ".py" or not path.is_file():
        raise ValueError("python candidate must be an existing .py file")
    if path != root and root not in path.parents:
        raise ValueError("python candidate must stay inside candidate_root")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, digest


def _run_fixture_worker(payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("fixture timeout must be positive")
    environment = {
        key: value
        for key in ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP")
        if (value := os.environ.get(key))
    }
    environment.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "RECRUITOPS_CANDIDATE_FIXTURE": "1",
    })
    completed = subprocess.run(
        [sys.executable, "-m", "packages.recruitment_core.candidate_worker"],
        cwd=Path(__file__).resolve().parents[2],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=timeout_seconds,
        check=False,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("candidate worker returned invalid JSON") from exc
    if completed.returncode != 0 or not isinstance(result, dict) or result.get("ok") is not True:
        message = result.get("error") if isinstance(result, dict) else None
        raise RuntimeError(str(message or "candidate worker failed")[-1_000:])
    return result


def _audit_fixture(
    spec: AdapterCandidateSpec,
    fixture: AdapterFixture,
    worker_result: dict[str, Any],
) -> AdapterFixtureResult:
    raw_jobs = worker_result.get("jobs")
    if not isinstance(raw_jobs, list) or not all(isinstance(job, dict) for job in raw_jobs):
        raise ValueError("candidate worker result requires a jobs list")
    jobs = [_normalized_job(spec.company, job) for job in raw_jobs]
    origin = urlsplit(spec.source_url)
    audit = accept_crawler_run(CrawlerAcceptanceInput(
        company=spec.company,
        source_url=spec.source_url,
        allowed_origins=[f"{origin.scheme}://{origin.netloc}"],
        jobs=jobs,
        pages_seen=int(worker_result.get("pages_seen") or 0),
        total_pages=worker_result.get("total_pages"),
        has_more=bool(worker_result.get("has_more", False)),
        pagination_complete=worker_result.get("pagination_complete"),
        advertised_total=worker_result.get("advertised_total"),
        expected_cohort=spec.expected_cohort,
        require_complete_jd=True,
    ))
    observed_titles = [job.title for job in jobs]
    titles_match = observed_titles == fixture.expected_titles
    passed = bool(audit.success and titles_match)
    error_code = None
    error_message = None
    if not titles_match:
        error_code = "fixture_title_mismatch"
        error_message = "Observed titles did not exactly match the frozen fixture expectation."
    elif not audit.success:
        error_code = str(audit.error_code.value) if audit.error_code is not None else "audit_failed"
        error_message = audit.error_message
    return AdapterFixtureResult(
        name=fixture.name,
        passed=passed,
        observed_titles=observed_titles,
        expected_titles=fixture.expected_titles,
        accepted_count=audit.data.accepted_count if audit.data is not None else 0,
        error_code=error_code,
        error_message=error_message,
    )


def accept_adapter_candidate(
    spec: AdapterCandidateSpec,
    *,
    candidate_root: Path,
    timeout_seconds: float = 10,
) -> AdapterCandidateAcceptance:
    """Run every frozen fixture in a child process and stop before activation."""

    python_path: Path | None = None
    source_sha256: str | None = None
    if spec.kind == "python":
        python_path, source_sha256 = _resolve_python_file(str(spec.python_file), candidate_root)

    results: list[AdapterFixtureResult] = []
    for fixture in spec.fixtures:
        request = {
            "kind": spec.kind,
            "company": spec.company,
            "source_url": spec.source_url,
            "recipe": spec.recipe,
            "python_file": str(python_path) if python_path else None,
            "source_sha256": source_sha256,
            "fixture": fixture.input,
        }
        try:
            worker_result = _run_fixture_worker(request, timeout_seconds)
            result = _audit_fixture(spec, fixture, worker_result)
        except (OSError, RuntimeError, subprocess.TimeoutExpired, TypeError, ValueError) as exc:
            result = AdapterFixtureResult(
                name=fixture.name,
                passed=False,
                expected_titles=fixture.expected_titles,
                error_code="isolated_fixture_failed",
                error_message=str(exc)[-1_000:],
            )
        results.append(result)
    state: CandidateReviewState = (
        "awaiting_approval"
        if results and all(item.passed for item in results)
        else "fixture_failed"
    )
    return AdapterCandidateAcceptance(
        candidate_id=_candidate_id(spec, source_sha256),
        company=spec.company,
        source_url=spec.source_url,
        kind=spec.kind,
        state=state,
        source_sha256=source_sha256,
        fixture_results=results,
    )


__all__ = [
    "AdapterCandidateAcceptance",
    "AdapterCandidateSpec",
    "AdapterFixture",
    "AdapterFixtureResult",
    "accept_adapter_candidate",
]
