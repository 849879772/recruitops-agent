"""Read and safely crawl companies discovered from the local OC snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, Callable, Literal, Protocol
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment
from pydantic import Field, field_validator, model_validator
import requests
import yaml

from packages.discovery import (
    SourceLead,
    classify_oc_destination_url,
    consolidate_source_leads,
    filter_oc_snapshot,
    reconcile_companies,
)
from packages.matching import is_doctorate_only, is_internship, is_jd_incomplete
from packages.recruitment_core import job_cohorts
from packages.recruitment_core.entry import (
    OcEntryDiagnosis,
    _COMPANIES_YAML_HOST_MAP,
    diagnose_candidate_entry,
    infer_candidate_crawler,
)
from packages.recruitment_core.entry_crawl import (
    crawl_with_entry_discovery,
    discover_recruitment_entries,
)
from packages.recruitment_core.crawlers.render import observe_page_with_network, render_page
from packages.recruitment_core.crawlers.generic_render import GenericRenderCrawler
from packages.recruitment_core.crawlers.declarative import json_path_get
from packages.recruitment_core.job_cohorts import OC_TRUSTED_SOURCE

from .crawler_audit import CrawlerAcceptanceInput, ObservedCrawlerJob, accept_crawler_run
from .crawler_audit import observed_ats_detail_urls as _observed_ats_detail_urls
from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolModel, ToolResponse, ToolStatus


def _hydrate_candidate_detail(job: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    # Import lazily: the pipeline also imports candidate entry diagnosis.
    from packages.pipeline.isolation import IsolatedOperationTimeout, fetch_job_detail_result_isolated

    try:
        return dict(fetch_job_detail_result_isolated(job, timeout_seconds=timeout_seconds))
    except IsolatedOperationTimeout as exc:
        return {"detail": "", "status": "timeout", "error_type": type(exc).__name__}



def _load_company_rows(path: Path) -> list[dict[str, Any]]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"Agent companies config does not exist: {resolved}")
    try:
        payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"unable to read Agent companies config: {resolved}") from exc
    rows = payload.get("companies") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Agent companies config must contain a companies list")
    return [dict(row) for row in rows]




def _is_addressable_candidate_url(url: str) -> bool:
    return diagnose_candidate_entry(url).entry_kind != "invalid_entry"


class OcCandidateListInput(ToolInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=25, ge=1, le=100)
    only_resolved: bool = False
    company_names: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("company_names")
    @classmethod
    def unique_company_names(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = value.strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result


class OcCandidateItem(ToolModel):
    company: str
    industries: list[str] = Field(default_factory=list)
    recruitment_types: list[str] = Field(default_factory=list)
    recruitment_targets: list[str] = Field(default_factory=list)
    resolved_urls: list[str] = Field(default_factory=list)
    inferred_crawlers: list[str] = Field(default_factory=list)
    source_rows: int = Field(ge=1)
    source_projects: list[str] = Field(default_factory=list)
    entry_diagnoses: list[OcEntryDiagnosis] = Field(default_factory=list)
    approval_state: Literal["not_addressable", "needs_isolated_test"] = "not_addressable"
    runtime_enabled: Literal[False] = False


class OcCandidateListData(ToolModel):
    total_candidates: int = Field(ge=0)
    resolved_candidates: int = Field(ge=0)
    unresolved_candidates: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    candidates: list[OcCandidateItem] = Field(default_factory=list)
    snapshot_captured_at: str | None = None


class OcCandidateListResponse(ToolResponse[OcCandidateListData]):
    pass


class OcCandidatePageObserveInput(ToolInput):
    company_name: str = Field(min_length=1, max_length=200)
    render_timeout_seconds: int = Field(default=75, ge=10, le=180)
    outline_limit: int = Field(default=120, ge=20, le=200)


class OcDomNodeOutline(ToolModel):
    tag: str
    element_id: str | None = None
    classes: list[str] = Field(default_factory=list, max_length=8)
    text: str = Field(max_length=240)
    href: str | None = None
    selector_hint: str


class OcSelectorCandidate(ToolModel):
    selector: str = Field(min_length=1, max_length=500)
    match_count: int = Field(ge=1, le=500)
    sample_titles: list[str] = Field(default_factory=list, max_length=5)


class OcJsonResponseCandidate(ToolModel):
    response_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    url: str = Field(max_length=2_048)
    method: str = Field(max_length=12)
    status: int = Field(ge=100, le=599)
    array_paths: dict[str, int] = Field(default_factory=dict)
    scalar_samples: dict[str, str] = Field(default_factory=dict)
    request_body: dict[str, Any] | list[Any] | None = None


class OcCandidatePageObserveData(ToolModel):
    company: str
    source_url: str
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    visible_text: str = Field(max_length=12_000)
    nodes: list[OcDomNodeOutline] = Field(default_factory=list, max_length=200)
    selector_candidates: list[OcSelectorCandidate] = Field(default_factory=list, max_length=3)
    json_candidates: list[OcJsonResponseCandidate] = Field(default_factory=list, max_length=10)
    untrusted_page_content: Literal[True] = True


class OcCandidatePageObserveResponse(ToolResponse[OcCandidatePageObserveData]):
    pass


class OcHtmlListRecipeInput(ToolModel):
    type: Literal["html_list"]
    listing_url: str | None = Field(default=None, max_length=2_048)
    list_selector: str = Field(min_length=1, max_length=500)
    title_selector: str = Field(min_length=1, max_length=500)
    detail_link_selector: str = Field(default="a[href]", min_length=1, max_length=500)
    jd_selector: str | None = Field(default=None, max_length=500)
    next_page_text: str | None = Field(default=None, max_length=100)
    next_page_selector: str | None = Field(default=None, max_length=500)
    max_pages: int = Field(default=20, ge=1, le=50)
    interactions: list[dict[Literal["text"], str]] = Field(default_factory=list, max_length=8)


class OcApiRequestInput(ToolModel):
    method: Literal["GET", "POST"] = "GET"
    url: str = Field(min_length=8, max_length=2_048)
    body: dict[str, Any] = Field(default_factory=dict)


class OcApiPaginationInput(ToolModel):
    page_key: str = Field(default="page", min_length=1, max_length=80)
    size_key: str = Field(default="pageSize", min_length=1, max_length=80)
    page_size: int = Field(default=30, ge=1, le=200)


class OcApiFieldMapInput(ToolModel):
    id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=120)
    city: str | None = Field(default=None, max_length=120)
    jd: str | list[str] = Field(default_factory=list)
    job_type: str | None = Field(default=None, max_length=120)
    published_at: str | None = Field(default=None, max_length=120)


class OcApiDetailInput(ToolModel):
    url_template: str = Field(min_length=8, max_length=2_048)
    record_path: str = Field(default="$.data", min_length=1, max_length=300)
    jd_fields: list[str] = Field(default_factory=list, max_length=20)


class OcApiScopeInput(ToolModel):
    include: bool = True
    label: str = Field(default="2027届校园招聘", max_length=120)
    evidence: str = Field(default="OC 2027届秋招", max_length=500)
    cohort: int = Field(default=2027, ge=1, le=9_999)
    body_overrides: dict[str, Any] = Field(default_factory=dict)


class OcApiCampaignRecipeInput(ToolModel):
    type: Literal["api_campaigns"]
    request: OcApiRequestInput
    items_path: str = Field(min_length=1, max_length=300)
    total_path: str | None = Field(default=None, max_length=300)
    field_map: OcApiFieldMapInput
    pagination: OcApiPaginationInput = Field(default_factory=OcApiPaginationInput)
    detail_url_template: str | None = Field(default=None, max_length=2_048)
    detail_api: OcApiDetailInput | None = None
    scopes: list[OcApiScopeInput] = Field(default_factory=lambda: [OcApiScopeInput()], max_length=10)


OcCandidateRecipeInput = Annotated[
    OcHtmlListRecipeInput | OcApiCampaignRecipeInput,
    Field(discriminator="type"),
]


class OcAdapterCandidateTestInput(ToolInput):
    company_name: str = Field(min_length=1, max_length=200)
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipe: OcCandidateRecipeInput | None = None
    title_selector: str | None = Field(default=None, min_length=1, max_length=500)
    interaction_texts: list[str] = Field(default_factory=list, max_length=8)
    expected_min_jobs: int = Field(default=1, ge=1, le=500)
    require_complete_jd: bool = True
    render_timeout_seconds: int = Field(default=120, ge=20, le=180)

    @model_validator(mode="after")
    def require_recipe_or_legacy_selector(self) -> "OcAdapterCandidateTestInput":
        if self.recipe is None and not self.title_selector:
            raise ValueError("recipe or legacy title_selector is required")
        return self


class OcAdapterCandidateTestData(ToolModel):
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    company: str
    source_url: str
    recipe_type: Literal["dom", "html_list", "api_campaigns"]
    title_selector: str | None = None
    state: Literal["needs_revision", "awaiting_approval"]
    raw_job_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    complete_jd_count: int = Field(ge=0)
    pagination_complete: bool
    titles: list[str] = Field(default_factory=list, max_length=50)
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list, max_length=20)
    error_code: str | None = None
    runtime_enabled: Literal[False] = False


class OcAdapterCandidateTestResponse(ToolResponse[OcAdapterCandidateTestData]):
    pass


class OcCandidateCrawlBatchInput(ToolInput):
    company_names: list[str] = Field(default_factory=list, max_length=3)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=3, ge=1, le=3)
    expected_cohort: int = Field(default=2027, ge=1, le=9_999)
    require_complete_jd: bool = False
    include_job_evidence: bool = False
    per_company_timeout_seconds: int = Field(default=75, ge=10, le=180)

    @field_validator("company_names")
    @classmethod
    def unique_company_names(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = value.strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result


class OcCandidateCrawlItem(ToolModel):
    company: str
    status: Literal["succeeded", "failed", "not_addressable", "form_application"]
    source_url: str | None = None
    discovered_entry_url: str | None = None
    effective_source_urls: list[str] = Field(default_factory=list)
    crawler_key: str | None = None
    integration_status: Literal[
        "connected_complete",
        "connected_partial",
        "needs_adapter",
        "invalid_entry",
        "jd_hydration_required",
        "no_eligible_jobs",
        "unresolved",
        "form_entry",
        "access_blocked",
    ] = "unresolved"
    raw_job_count: int = Field(default=0, ge=0)
    accepted_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    complete_jd_count: int = Field(default=0, ge=0)
    incomplete_jd_count: int = Field(default=0, ge=0)
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    source_projects: list[str] = Field(default_factory=list)
    job_evidence: list["OcCandidateJobEvidence"] = Field(default_factory=list)
    composite_cohort_count: int = Field(default=0, ge=0)
    cohort_evidence: list[str] = Field(default_factory=list, max_length=5)
    pagination_complete: bool | None = None
    completeness_known: bool = False
    pagination_state: Literal["complete", "incomplete", "unknown"] = "unknown"
    pages_seen: int = Field(default=0, ge=0)
    total_pages: int | None = Field(default=None, ge=0)
    has_more: bool = False
    advertised_total: int | None = Field(default=None, ge=0)
    termination_reasons: list[str] = Field(default_factory=list, max_length=20)
    error_code: str | None = None
    error_message: str | None = None
    candidate_kind: Literal["reuse", "declarative", "python"] | None = None
    approval_state: Literal[
        "not_addressable",
        "needs_candidate",
        "isolated_test_failed",
        "awaiting_approval",
        "rejected",
    ] = "not_addressable"
    runtime_enabled: Literal[False] = False


class OcCandidateJobEvidence(ToolModel):
    job_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_job_id: str
    title: str
    city: str | None = None
    detail_url: str
    jd_chars: int = Field(ge=0)
    jd_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    jd_complete: bool
    accepted: bool
    cohort: int | None = None
    cohort_status: str
    batch: str


class OcCandidateCrawlBatchData(ToolModel):
    total_candidates: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    addressable_count: int = Field(ge=0)
    attempted_count: int = Field(ge=0)
    succeeded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    not_addressable_count: int = Field(ge=0)
    complete_count: int = Field(default=0, ge=0)
    partial_count: int = Field(default=0, ge=0)
    needs_adapter_count: int = Field(default=0, ge=0)
    invalid_entry_count: int = Field(default=0, ge=0)
    results: list[OcCandidateCrawlItem] = Field(default_factory=list)


class OcCandidateCrawlBatchResponse(ToolResponse[OcCandidateCrawlBatchData]):
    pass


class CandidateCrawlerProcess(Protocol):
    def __call__(
        self,
        *,
        company: str,
        crawler_key: str,
        source_url: str,
        timeout_seconds: float,
        source_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]: ...


class SubprocessCandidateCrawlerProcess:
    """Run each candidate in a killable child process with no configuration write."""

    def __call__(
        self,
        *,
        company: str,
        crawler_key: str,
        source_url: str,
        timeout_seconds: float,
        source_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "name": company,
            "crawler": crawler_key,
            "careers_url": source_url,
        }
        payload.update(dict(source_context or {}))
        environment = dict(os.environ)
        environment["RECRUITOPS_CRAWL_TIMEOUT_SECONDS"] = (
            f"{max(0.001, float(timeout_seconds) - 5.0):.6f}"
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_agent_crawler",
                "--company",
                json.dumps(payload, ensure_ascii=False),
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "crawler process failed").strip()
            raise RuntimeError(detail[-1_000:])
        result = json.loads(completed.stdout)
        jobs = result.get("jobs") if isinstance(result, dict) else None
        if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
            raise RuntimeError("crawler process returned invalid jobs")
        return dict(result)


def _process_result(value: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize production evidence while keeping lightweight test doubles usable."""

    if isinstance(value, list):
        return {
            "jobs": value,
            "pagination_complete": True,
            "completeness_known": True,
            "pages_seen": 1,
            "total_pages": 1,
            "has_more": False,
            "advertised_total": len(value),
            "termination_reasons": ["test_process_complete"],
        }
    jobs = value.get("jobs")
    if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
        raise ValueError("candidate crawler result must contain a jobs list")
    return value


def _empty_result_status(
    source_url: str,
    diagnosis: OcEntryDiagnosis | None = None,
    process_result: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    if (process_result or {}).get("error_code"):
        code = str(process_result["error_code"])
        if code in {"login_required", "captcha_required", "access_denied"}:
            status = "access_blocked"
        elif code in {"invalid_entry", "form_application_only"}:
            status = "form_entry" if code == "form_application_only" else "invalid_entry"
        elif code == "activity_empty":
            status = "no_eligible_jobs"
        else:
            status = "needs_adapter"
        return status, code, str(process_result.get("error_message") or code)
    excluded = classify_oc_destination_url(source_url)
    if excluded is not None:
        kind, reason = excluded
        return "invalid_entry", str(kind), reason
    diagnosis = diagnosis or diagnose_candidate_entry(source_url)
    host = (urlsplit(source_url).hostname or "").casefold()
    if host.endswith(".xinrenxinshi.com") or host == "s.xinrenxinshi.com":
        return "needs_adapter", "xinrenxinshi_adapter_required", "The Xinrenxinshi portal needs a reusable platform adapter."
    if host.endswith(".51job.com") or host == "51job.com":
        return "needs_adapter", "51job_adapter_required", "The 51job campaign needs a reusable platform adapter or campaign parser."
    if diagnosis.entry_kind == "existing_adapter":
        crawler_key = diagnosis.crawler_key or "unknown"
        evidence = process_result or {}
        if (crawler_key == "moka" and evidence.get("advertised_total") == 0
                and evidence.get("pagination_complete") is True
                and evidence.get("completeness_known") is True
                and not evidence.get("has_more")):
            return (
                "no_eligible_jobs",
                "activity_empty",
                "The campaign reports zero jobs with complete pagination evidence.",
            )
        return (
            "needs_adapter",
            "adapter_variant_unsupported",
            f"The {crawler_key} adapter matched this destination but returned no jobs.",
        )
    if diagnosis.entry_kind == "entry_discovery_required":
        return "needs_adapter", "recruitment_entry_discovery_required", "The URL is a company/home entry and needs recruitment-page discovery."
    if diagnosis.entry_kind == "form_application":
        return "form_entry", "form_application_only", "The URL is a form or document for manual application submission, not a crawlable job listing."
    return "needs_adapter", "site_adapter_required", "The recruitment page needs a reusable parser or browser adapter."


def _discover_recruitment_entries(source_url: str, timeout_seconds: float) -> list[str]:
    """Compatibility wrapper using the shared core discovery implementation."""
    return discover_recruitment_entries(
        source_url, timeout_seconds, render=render_page, http_get=requests.get,
    )


def _normalized_job(company: str, job: dict[str, Any]) -> ObservedCrawlerJob:
    detail_url = str(job.get("detail_url") or job.get("jd_url") or "")
    configured_id = str(job.get("id") or job.get("source_job_id") or "").strip()
    identity = configured_id or hashlib.sha256(
        "\x00".join(
            (
                company,
                detail_url,
                str(job.get("title") or ""),
                str(job.get("city") or ""),
            )
        ).encode("utf-8")
    ).hexdigest()
    cohort = job.get("cohort")
    try:
        parsed_cohort = int(cohort) if cohort is not None and str(cohort).strip() else None
        cohort = parsed_cohort if parsed_cohort is not None and parsed_cohort > 0 else None
    except (TypeError, ValueError):
        cohort = None
    batch = str(job.get("batch") or job.get("recruitment_track") or "unknown").strip()
    batch = {"early_batch": "early"}.get(batch, batch)
    return ObservedCrawlerJob(
        id=identity,
        title=str(job.get("title") or ""),
        city=str(job.get("city") or "") or None,
        detail_url=detail_url,
        jd_raw=str(job.get("jd_raw") or "") or None,
        capture_evidence=job.get("capture_evidence") or {},
        cohort=cohort,
        cohort_status=str(job.get("cohort_status") or "unconfirmed"),
        cohort_source=str(job.get("cohort_source") or "") or None,
        cohort_evidence=str(job.get("cohort_evidence") or "") or None,
        batch=batch if batch in {"formal", "early", "internship", "unknown"} else "unknown",
    )


class OcCandidateRunner:
    def __init__(
        self,
        snapshot_path: Path,
        companies_config: Path,
        process: CandidateCrawlerProcess | None = None,
        live_candidate_process: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.snapshot_path = snapshot_path.expanduser().resolve()
        self.companies_config = companies_config.expanduser().resolve()
        self.process = process or SubprocessCandidateCrawlerProcess()
        self.live_candidate_process = live_candidate_process or self._run_live_candidate_process

    @staticmethod
    def _run_live_candidate_process(
        *,
        company: str,
        source_url: str,
        recipe: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        environment = {
            key: value
            for key in (
                "SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP",
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                "RECRUITOPS_BROWSER_CHANNEL", "RECRUITOPS_BROWSER_EXECUTABLE_PATH",
            )
            if (value := os.environ.get(key))
        }
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        completed = subprocess.run(
            [sys.executable, "-m", "packages.recruitment_core.candidate_live_worker"],
            cwd=Path(__file__).resolve().parents[2],
            input=json.dumps({
                "company": company,
                "source_url": source_url,
                "recipe": recipe,
            }, ensure_ascii=False),
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
            raise RuntimeError("candidate live worker returned invalid JSON") from exc
        if completed.returncode != 0 or result.get("ok") is not True:
            raise RuntimeError(str(result.get("error") or "candidate live worker failed")[-1_000:])
        return dict(result)

    @property
    def candidate_observation_root(self) -> Path:
        return self.snapshot_path.parent / "adapter_candidates" / "observations"

    def _new_candidates(self):
        snapshot = filter_oc_snapshot(self.snapshot_path)
        leads = consolidate_source_leads(snapshot.leads)
        company_rows = _load_company_rows(self.companies_config)
        reconciliation = reconcile_companies(
            leads,
            company_rows,
        )
        pending_names = {
            str(row.get("name") or "").strip()
            for row in company_rows
            if str(row.get("integration_status") or "connected") == "not_connected"
        }
        candidates = list(reconciliation.new)
        seen = {lead.canonical_name for lead in candidates}
        for lead in reconciliation.existing:
            matched_name = str(lead.matched_company or "").strip()
            if matched_name not in pending_names or matched_name in seen:
                continue
            candidates.append(replace(lead, canonical_name=matched_name))
            seen.add(matched_name)
        pending_by_identity = {
            str(row.get("source_identity") or "").strip(): str(row.get("name") or "").strip()
            for row in company_rows
            if str(row.get("integration_status") or "connected") == "not_connected"
            and str(row.get("source_identity") or "").strip()
        }
        for lead in reconciliation.ambiguous:
            matched_name = pending_by_identity.get(str(lead.source_identity or "").strip())
            if not matched_name or matched_name in seen:
                continue
            candidates.append(replace(lead, canonical_name=matched_name))
            seen.add(matched_name)
        candidates.sort(key=lambda item: item.canonical_name.casefold())
        return snapshot, candidates

    def _candidate_lead(self, company_name: str):
        _, leads = self._new_candidates()
        target = company_name.strip().casefold()
        matches = [
            lead for lead in leads
            if lead.canonical_name.casefold() == target
            or any(name.casefold() == target for name in self._project_names(lead))
        ]
        if len(matches) != 1:
            raise ValueError(
                "OC candidate company was not uniquely resolved"
                if matches
                else "OC candidate company was not found"
            )
        return matches[0]

    @staticmethod
    def _public_source_url(lead) -> str:
        urls = [url for url in lead.source_urls if _is_addressable_candidate_url(url)]
        if not urls:
            raise ValueError("OC candidate has no resolved public recruitment URL")
        return urls[0]

    @staticmethod
    def _sanitize_observation_html(page_html: str) -> str:
        soup = BeautifulSoup(page_html or "", "html.parser")
        for node in soup.select("script, style, noscript, template"):
            node.decompose()
        for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
            comment.extract()
        for node in soup.find_all(True):
            for attribute in list(node.attrs):
                if attribute.casefold().startswith("on") or attribute.casefold() in {
                    "value", "srcdoc", "nonce", "integrity",
                }:
                    del node.attrs[attribute]
        return str(soup)[:2_000_000]

    @staticmethod
    def _selector_hint(node) -> str:
        tag = str(node.name or "*")
        element_id = str(node.get("id") or "").strip()
        if element_id and re.fullmatch(r"[A-Za-z_][\w-]*", element_id):
            return f"{tag}#{element_id}"
        classes = [
            str(value) for value in (node.get("class") or [])
            if re.fullmatch(r"[A-Za-z_][\w-]*", str(value))
        ]
        if classes:
            return f"{tag}.{classes[0]}"
        href = str(node.get("href") or "").strip()
        if node.name == "a" and href:
            stable = href.split("?", 1)[0]
            if stable:
                return f'a[href^="{stable[:160]}"]'
        return tag

    @staticmethod
    def _selector_candidates(page_html: str, source_url: str) -> list[OcSelectorCandidate]:
        crawler = GenericRenderCrawler("candidate-observation", source_url)
        soup = BeautifulSoup(page_html or "", "html.parser")
        grouped: dict[str, list[str]] = {}
        for node in soup.find_all(["a", "h2", "h3", "h4", "h5", "span", "div", "p", "li"]):
            title = crawler._clean_title(crawler._direct_text(node))
            if not title:
                continue
            grouped.setdefault(crawler._sig(node), []).append(title)
        ranked = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
        return [
            OcSelectorCandidate(
                selector=selector,
                match_count=len(titles),
                sample_titles=list(dict.fromkeys(titles))[:5],
            )
            for selector, titles in ranked[:3]
            if titles
        ]

    def observe_page(
        self,
        request: OcCandidatePageObserveInput,
    ) -> OcCandidatePageObserveResponse:
        started = perf_counter()
        try:
            lead = self._candidate_lead(request.company_name)
            source_url = self._public_source_url(lead)
            observation = observe_page_with_network(
                source_url,
                timeout_ms=request.render_timeout_seconds * 1000,
                extra_wait_ms=3500,
                scroll_times=4,
            )
            page_html = observation.get("html")
            if not str(page_html or "").strip():
                response = requests.get(
                    source_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                        ),
                        "Accept-Language": "zh-CN,zh;q=0.9",
                    },
                    timeout=min(request.render_timeout_seconds, 30),
                )
                response.raise_for_status()
                response.encoding = response.apparent_encoding or response.encoding
                page_html = response.text
            sanitized = self._sanitize_observation_html(page_html)
            if not sanitized.strip():
                raise ValueError("rendered recruitment page was empty")
            snapshot_id = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
            root = self.candidate_observation_root
            root.mkdir(parents=True, exist_ok=True)
            (root / f"{snapshot_id}.html").write_text(sanitized, encoding="utf-8")
            network_responses = list(observation.get("responses") or [])[:20]
            (root / f"{snapshot_id}.json").write_text(json.dumps({
                "company": lead.canonical_name,
                "source_url": source_url,
                "sha256": snapshot_id,
                "network_responses": network_responses,
            }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            soup = BeautifulSoup(sanitized, "html.parser")
            candidates = []
            for node in soup.find_all(["a", "button", "h1", "h2", "h3", "h4", "li", "tr"]):
                text = " ".join(node.get_text(" ", strip=True).split())
                if not 2 <= len(text) <= 240:
                    continue
                href = str(node.get("href") or "").strip()
                score = 0
                signal = f"{text} {href}".casefold()
                if re.search(r"岗位|工程师|招聘|职位|job|career|campus", signal, re.I):
                    score += 4
                if href:
                    score += 2
                if node.name in {"h2", "h3", "h4"}:
                    score += 1
                candidates.append((score, OcDomNodeOutline(
                    tag=str(node.name),
                    element_id=str(node.get("id") or "") or None,
                    classes=[str(value) for value in (node.get("class") or [])][:8],
                    text=text,
                    href=urljoin(source_url, href) if href else None,
                    selector_hint=self._selector_hint(node),
                )))
            candidates.sort(key=lambda item: item[0], reverse=True)
            visible_text = " ".join(soup.get_text(" ", strip=True).split())[:12_000]
            return OcCandidatePageObserveResponse(
                tool_name="oc_candidate_page_observe",
                status=ToolStatus.SUCCESS,
                success=True,
                data=OcCandidatePageObserveData(
                    company=lead.canonical_name,
                    source_url=source_url,
                    snapshot_id=snapshot_id,
                    visible_text=visible_text,
                    nodes=[item for _, item in candidates[:request.outline_limit]],
                    selector_candidates=self._selector_candidates(sanitized, source_url),
                    json_candidates=[OcJsonResponseCandidate(
                        response_id=str(item.get("sha256") or ""),
                        url=str(item.get("url") or ""),
                        method=str(item.get("method") or "GET"),
                        status=int(item.get("status") or 200),
                        array_paths={
                            str(key): int(value)
                            for key, value in list((item.get("array_paths") or {}).items())[:20]
                        },
                        scalar_samples={
                            str(key): str(value)[:160]
                            for key, value in list((item.get("scalar_samples") or {}).items())[:30]
                        },
                        request_body=(
                            item.get("request_body")
                            if isinstance(item.get("request_body"), (dict, list))
                            else None
                        ),
                    ) for item in network_responses[:10]],
                ),
                evidence=[EvidenceSource(source="oc_candidate_page", source_ref=source_url)],
                timeout_ms=request.timeout_ms,
                elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return OcCandidatePageObserveResponse(
                tool_name="oc_candidate_page_observe",
                status=ToolStatus.FAILURE,
                success=False,
                evidence=[EvidenceSource(source="oc_snapshot", source_ref=str(self.snapshot_path))],
                error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
                error_message=str(exc)[-1_000:],
                timeout_ms=request.timeout_ms,
                elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
            )

    def test_dom_candidate(
        self,
        request: OcAdapterCandidateTestInput,
    ) -> OcAdapterCandidateTestResponse:
        started = perf_counter()
        source_url = ""
        try:
            lead = self._candidate_lead(request.company_name)
            source_url = self._public_source_url(lead)
            metadata_path = self.candidate_observation_root / f"{request.snapshot_id}.json"
            html_path = self.candidate_observation_root / f"{request.snapshot_id}.html"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            frozen_html = html_path.read_text(encoding="utf-8")
            if hashlib.sha256(frozen_html.encode("utf-8")).hexdigest() != request.snapshot_id:
                raise ValueError("candidate observation digest mismatch")
            if metadata.get("company") != lead.canonical_name or metadata.get("source_url") != source_url:
                raise ValueError("candidate observation does not belong to the requested company")
            frozen_soup = BeautifulSoup(frozen_html, "html.parser")
            if request.recipe is None:
                recipe = {
                    "type": "dom",
                    "listing_url": source_url,
                    "title_selector": request.title_selector,
                    "interactions": [{"text": value} for value in request.interaction_texts],
                }
                frozen_count = len(frozen_soup.select(str(request.title_selector)))
            else:
                recipe = request.recipe.model_dump(mode="json", exclude_none=True)
                if recipe["type"] == "html_list":
                    cards = frozen_soup.select(str(recipe["list_selector"]))
                    frozen_count = sum(
                        card.select_one(str(recipe["title_selector"])) is not None
                        for card in cards
                    )
                else:
                    responses = metadata.get("network_responses") or []
                    matched = next((
                        item for item in responses
                        if str(item.get("url") or "") == str(recipe["request"]["url"])
                        and str(item.get("method") or "GET").upper()
                        == str(recipe["request"].get("method") or "GET").upper()
                    ), None)
                    if matched is None:
                        raise ValueError("API recipe does not match a frozen network response")
                    frozen_rows = json_path_get(matched.get("payload"), str(recipe["items_path"]))
                    frozen_count = len(frozen_rows) if isinstance(frozen_rows, list) else 0
            if frozen_count < request.expected_min_jobs:
                raise ValueError(
                    f"candidate matched {frozen_count} frozen jobs; expected at least "
                    f"{request.expected_min_jobs}"
                )
            if frozen_count > 500:
                raise ValueError("candidate matched more than 500 frozen jobs; refine the recipe")
            live_result = self.live_candidate_process(
                company=lead.canonical_name,
                source_url=source_url,
                recipe=recipe,
                timeout_seconds=request.render_timeout_seconds,
            )
            raw_jobs = list(live_result.get("jobs") or [])
            trusted = job_cohorts.trusted_source_campaign({
                "source_cohort": 2027,
                "source_cohort_source": OC_TRUSTED_SOURCE,
                "source_cohort_evidence": "OC固定筛选记录：招聘对象=2027届",
                "source_cohort_url": source_url,
            }, jobs_observed=bool(raw_jobs))
            classified = job_cohorts.annotate_company_jobs(
                raw_jobs,
                source_url,
                inspect_page=False,
                campaign=trusted or job_cohorts.unknown_cohort(campaign_url=source_url),
            )
            observed = [_normalized_job(lead.canonical_name, job) for job in classified]
            audit = accept_crawler_run(CrawlerAcceptanceInput(
                company=lead.canonical_name,
                source_url=source_url,
                allowed_origins=[f"{urlsplit(source_url).scheme}://{urlsplit(source_url).netloc}"],
                jobs=observed,
                pages_seen=1,
                total_pages=1,
                has_more=False,
                pagination_complete=bool(live_result.get("pagination_complete")),
                advertised_total=int(live_result.get("advertised_total") or len(observed)),
                expected_cohort=2027,
                require_complete_jd=request.require_complete_jd,
                timeout_ms=request.timeout_ms,
            ))
            data = audit.data
            accepted_count = data.accepted_count if data is not None else 0
            complete_jd_count = sum(not is_jd_incomplete(job) for job in observed)
            candidate_payload = json.dumps({
                "company": lead.canonical_name,
                "source_url": source_url,
                "snapshot_id": request.snapshot_id,
                "recipe": recipe,
            }, ensure_ascii=False, sort_keys=True)
            candidate_id = hashlib.sha256(candidate_payload.encode("utf-8")).hexdigest()
            passed = bool(
                audit.success
                and len(observed) >= request.expected_min_jobs
                and (not request.require_complete_jd or complete_jd_count == len(observed))
            )
            diagnostics = [f"title coverage: {len(observed)}/{max(frozen_count, len(observed))}"]
            missing_jd = len(observed) - complete_jd_count
            if missing_jd:
                diagnostics.append(
                    f"JD missing or incomplete: {missing_jd}/{len(observed)}; add a detail source"
                )
            if not bool(live_result.get("pagination_complete")):
                diagnostics.append("pagination did not reach a verified terminal condition")
            if data is not None:
                diagnostics.extend(
                    f"rejected {count}: {reason}"
                    for reason, count in sorted(data.rejection_reasons.items())
                )
            test_root = self.candidate_observation_root.parent / "tests"
            test_root.mkdir(parents=True, exist_ok=True)
            test_record = {
                "candidate_id": candidate_id,
                "company": lead.canonical_name,
                "source_url": source_url,
                "snapshot_id": request.snapshot_id,
                "recipe": recipe,
                "passed": passed,
                "state": "awaiting_approval" if passed else "needs_revision",
                "raw_job_count": len(observed),
                "accepted_count": accepted_count,
                "complete_jd_count": complete_jd_count,
                "pagination_complete": bool(live_result.get("pagination_complete")),
                "diagnostics": diagnostics[:20],
                "rejection_reasons": data.rejection_reasons if data is not None else {},
            }
            temporary = test_root / f".{candidate_id}.tmp"
            temporary.write_text(
                json.dumps(test_record, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(test_root / f"{candidate_id}.json")
            return OcAdapterCandidateTestResponse(
                tool_name="oc_adapter_candidate_test",
                status=ToolStatus.SUCCESS if passed else ToolStatus.FAILURE,
                success=passed,
                data=OcAdapterCandidateTestData(
                    candidate_id=candidate_id,
                    company=lead.canonical_name,
                    source_url=source_url,
                    recipe_type=str(recipe["type"]),
                    title_selector=str(recipe.get("title_selector") or "") or None,
                    state="awaiting_approval" if passed else "needs_revision",
                    raw_job_count=len(observed),
                    accepted_count=accepted_count,
                    complete_jd_count=complete_jd_count,
                    pagination_complete=bool(live_result.get("pagination_complete")),
                    titles=[job.title for job in observed[:50]],
                    rejection_reasons=data.rejection_reasons if data is not None else {},
                    diagnostics=diagnostics[:20],
                    error_code=str(audit.error_code.value) if audit.error_code is not None else None,
                ),
                evidence=[
                    EvidenceSource(source="frozen_dom_observation", source_ref=request.snapshot_id),
                    EvidenceSource(source="live_candidate_run", source_ref=source_url),
                ],
                error_code=audit.error_code,
                error_message=audit.error_message,
                timeout_ms=request.timeout_ms,
                elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired, TypeError, ValueError) as exc:
            return OcAdapterCandidateTestResponse(
                tool_name="oc_adapter_candidate_test",
                status=ToolStatus.FAILURE,
                success=False,
                evidence=[EvidenceSource(source="oc_candidate_page", source_ref=source_url or None)],
                error_code=ToolErrorCode.INVALID_INPUT,
                error_message=str(exc)[-1_000:],
                timeout_ms=request.timeout_ms,
                elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
            )

    @staticmethod
    def _project_names(lead) -> list[str]:
        return list(lead.metadata.get("source_project_names") or [lead.canonical_name])

    @staticmethod
    def _item(lead) -> OcCandidateItem:
        urls = [url for url in lead.source_urls if _is_addressable_candidate_url(url)]
        diagnoses = [diagnose_candidate_entry(url) for url in lead.source_urls]
        crawlers = sorted(
            {key for url in urls if (key := infer_candidate_crawler(url)) is not None}
        )
        return OcCandidateItem(
            company=lead.canonical_name,
            industries=list(lead.metadata.get("industries") or []),
            recruitment_types=list(lead.metadata.get("recruitment_types") or []),
            recruitment_targets=list(lead.metadata.get("recruitment_targets") or []),
            resolved_urls=urls,
            inferred_crawlers=crawlers,
            source_rows=int(lead.metadata.get("source_rows") or 1),
            source_projects=OcCandidateRunner._project_names(lead),
            entry_diagnoses=diagnoses,
            approval_state="needs_isolated_test" if urls else "not_addressable",
        )

    @staticmethod
    def _job_evidence(
        jobs: list[ObservedCrawlerJob],
        accepted_ids: set[str],
    ) -> list[OcCandidateJobEvidence]:
        evidence: list[OcCandidateJobEvidence] = []
        for job in jobs:
            jd = str(job.jd_raw or "")
            identity = "\x00".join((job.id, job.detail_url, job.title, str(job.city or "")))
            evidence.append(OcCandidateJobEvidence(
                job_key=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                source_job_id=job.id,
                title=job.title,
                city=job.city,
                detail_url=job.detail_url,
                jd_chars=len(jd),
                jd_sha256=hashlib.sha256(jd.encode("utf-8")).hexdigest() if jd else None,
                jd_complete=not is_jd_incomplete(job),
                accepted=job.id in accepted_ids,
                cohort=job.cohort,
                cohort_status=job.cohort_status,
                batch=str(job.batch.value if hasattr(job.batch, "value") else job.batch),
            ))
        return evidence

    def list(self, request: OcCandidateListInput) -> OcCandidateListResponse:
        started = perf_counter()
        try:
            snapshot, leads = self._new_candidates()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return OcCandidateListResponse(
                tool_name="oc_candidate_list",
                status=ToolStatus.FAILURE,
                success=False,
                evidence=[EvidenceSource(source="oc_snapshot", source_ref=str(self.snapshot_path))],
                error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
                error_message=f"OC candidate list unavailable: {exc}",
                timeout_ms=request.timeout_ms,
                elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
            )
        requested_names = {name.casefold() for name in request.company_names}
        if requested_names:
            leads = [
                lead for lead in leads
                if lead.canonical_name.casefold() in requested_names
                or any(
                    name.casefold() in requested_names
                    for name in self._project_names(lead)
                )
            ]
        items = [self._item(lead) for lead in leads]
        resolved_count = sum(bool(item.resolved_urls) for item in items)
        visible = [item for item in items if item.resolved_urls or not request.only_resolved]
        page = visible[request.offset : request.offset + request.limit]
        return OcCandidateListResponse(
            tool_name="oc_candidate_list",
            status=ToolStatus.SUCCESS,
            success=True,
            data=OcCandidateListData(
                total_candidates=len(items),
                resolved_candidates=resolved_count,
                unresolved_candidates=len(items) - resolved_count,
                offset=request.offset,
                limit=request.limit,
                candidates=page,
                snapshot_captured_at=snapshot.captured_at,
            ),
            evidence=[EvidenceSource(source="oc_snapshot", source_ref=str(self.snapshot_path))],
            timeout_ms=request.timeout_ms,
            elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
        )

    def _crawl_one(
        self,
        lead,
        request: OcCandidateCrawlBatchInput,
        *,
        hydrate_details: bool = False,
    ) -> OcCandidateCrawlItem:
        urls = [url for url in lead.source_urls if _is_addressable_candidate_url(url)]
        if not urls:
            return OcCandidateCrawlItem(
                company=lead.canonical_name,
                status="not_addressable",
                integration_status="unresolved",
                source_projects=self._project_names(lead),
                error_code="unresolved_apply_url",
                error_message="No resolved public recruitment URL is available.",
            )
        source_url = original_source_url = urls[0]
        diagnosis = diagnose_candidate_entry(source_url)

        # Early exit for form/doc applications - no crawling needed
        if diagnosis.entry_kind == "form_application":
            return OcCandidateCrawlItem(
                company=lead.canonical_name,
                status="form_application",
                integration_status="form_entry",
                source_url=source_url,
                source_projects=self._project_names(lead),
                error_code="form_application_only",
                error_message=diagnosis.reason,
            )

        crawler_key = diagnosis.crawler_key or (
            "render" if diagnosis.entry_kind == "entry_discovery_required" else None
        )
        discovered_entry_url = None

        def source_context(url: str) -> dict[str, Any] | None:
            if request.expected_cohort != 2027:
                return None
            return {
                "source_cohort": request.expected_cohort,
                "source_cohort_source": OC_TRUSTED_SOURCE,
                "source_cohort_evidence": (
                    "OC固定筛选记录：招聘对象="
                    f"{','.join(lead.metadata.get('recruitment_targets') or [])}；"
                    "招聘类型="
                    f"{','.join(lead.metadata.get('recruitment_types') or [])}"
                ),
                "source_cohort_url": url,
            }
        try:
            def crawl_entry(url: str, key: str, remaining: float) -> dict[str, Any]:
                return _process_result(self.process(
                    company=lead.canonical_name,
                    crawler_key=key,
                    source_url=url,
                    timeout_seconds=remaining,
                    source_context=source_context(original_source_url),
                ))

            process_result = crawl_with_entry_discovery(
                original_source_url,
                crawl=crawl_entry,
                crawler_key=crawler_key,
                timeout_seconds=request.per_company_timeout_seconds,
                discover=_discover_recruitment_entries,
            )
            source_url = process_result["crawl_source_url"]
            discovered_entry_url = process_result["discovered_entry_url"]
            crawler_key = process_result["crawler_key"]
            diagnosis = diagnose_candidate_entry(source_url)
            raw_jobs = list(process_result["jobs"])
            observed = [_normalized_job(lead.canonical_name, job) for job in raw_jobs]
            origin = urlsplit(source_url)
            allowed_origins = [f"{origin.scheme}://{origin.netloc}"]
            for effective_url in process_result.get("effective_source_urls") or []:
                effective = urlsplit(str(effective_url))
                if effective.scheme in {"http", "https"} and effective.netloc:
                    effective_origin = f"{effective.scheme}://{effective.netloc}"
                    if effective_origin not in allowed_origins:
                        allowed_origins.append(effective_origin)
            source_host = (origin.hostname or "").casefold()
            allowed_detail_urls = _observed_ats_detail_urls(raw_jobs, source_url, process_result)
            if source_host.endswith(".51job.com"):
                for job in observed:
                    detail = urlsplit(job.detail_url)
                    if (
                        detail.scheme in {"http", "https"}
                        and (detail.hostname or "").casefold().endswith(".51job.com")
                    ):
                        detail_origin = f"{detail.scheme}://{detail.netloc}"
                        if detail_origin not in allowed_origins:
                            allowed_origins.append(detail_origin)
            hydration_candidate_count = 0
            hydration_outcomes: dict[str, int] = {}
            pending_details: list[tuple[dict[str, Any], ObservedCrawlerJob]] = []
            if request.require_complete_jd and observed:
                preliminary = accept_crawler_run(
                    CrawlerAcceptanceInput(
                        company=lead.canonical_name,
                        source_url=source_url,
                        allowed_origins=allowed_origins,
                        allowed_detail_urls=allowed_detail_urls,
                        jobs=observed,
                        pages_seen=int(process_result.get("pages_seen") or 0),
                        total_pages=process_result.get("total_pages"),
                        has_more=bool(process_result.get("has_more", False)),
                        pagination_complete=process_result.get("pagination_complete"),
                        completeness_known=process_result.get("completeness_known"),
                        advertised_total=process_result.get("advertised_total"),
                        expected_cohort=request.expected_cohort,
                        require_complete_jd=False,
                        timeout_ms=request.timeout_ms,
                    )
                )
                eligible_ids = {
                    job.id
                    for job in (preliminary.data.accepted_jobs if preliminary.data else [])
                }
                pending_details = [
                    (raw, normalized)
                    for raw, normalized in zip(raw_jobs, observed, strict=True)
                    if (
                        normalized.id in eligible_ids
                        and is_jd_incomplete(normalized)
                        and not is_internship(normalized)
                        and not is_doctorate_only(normalized)
                    )
                ]
                hydration_candidate_count = len(pending_details)

            if hydrate_details and pending_details:
                detail_deadline = perf_counter() + request.per_company_timeout_seconds

                def hydrate(
                    pair: tuple[dict[str, Any], ObservedCrawlerJob],
                ) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
                    raw, normalized = pair
                    candidate = {
                        **raw,
                        "title": normalized.title,
                        "jd_url": normalized.detail_url,
                        "cohort": normalized.cohort,
                        "cohort_status": normalized.cohort_status,
                        "company": lead.canonical_name,
                        "careers_url": source_url,
                    }
                    remaining = detail_deadline - perf_counter()
                    if remaining <= 0:
                        return raw, "", "budget_exhausted", {}
                    result = _hydrate_candidate_detail(candidate, min(45.0, remaining))
                    outcome = str(result.get("status") or "fetch_failed")
                    detail = str(result.get("detail") or "")
                    capture = result.get("capture_evidence") or {}
                    hydrated = {**candidate, "jd_raw": detail, "capture_evidence": capture}
                    if outcome == "complete" and is_jd_incomplete(hydrated):
                        outcome = "content_incomplete"
                    if outcome != "complete" or is_jd_incomplete(hydrated):
                        detail = ""
                    return raw, detail, outcome, capture

                executor = ThreadPoolExecutor(max_workers=min(2, len(pending_details) or 1))
                futures = [executor.submit(hydrate, pair) for pair in pending_details]
                done, unfinished = wait(
                    futures,
                    timeout=request.per_company_timeout_seconds,
                )
                for future in done:
                    try:
                        raw, detail, outcome, capture = future.result()
                    except Exception:  # noqa: BLE001
                        hydration_outcomes["failed"] = hydration_outcomes.get("failed", 0) + 1
                        continue
                    hydration_outcomes[outcome] = hydration_outcomes.get(outcome, 0) + 1
                    if detail:
                        raw["jd_raw"] = detail
                        raw["capture_evidence"] = capture
                if unfinished:
                    hydration_outcomes["timeout"] = len(unfinished)
                executor.shutdown(wait=False, cancel_futures=True)
                observed = [_normalized_job(lead.canonical_name, job) for job in raw_jobs]
            audit = accept_crawler_run(
                CrawlerAcceptanceInput(
                    company=lead.canonical_name,
                    source_url=source_url,
                    allowed_origins=allowed_origins,
                    allowed_detail_urls=allowed_detail_urls,
                    jobs=observed,
                    pages_seen=int(process_result.get("pages_seen") or 0),
                    total_pages=process_result.get("total_pages"),
                    has_more=bool(process_result.get("has_more", False)),
                    pagination_complete=process_result.get("pagination_complete"),
                    completeness_known=process_result.get("completeness_known"),
                    advertised_total=process_result.get("advertised_total"),
                    expected_cohort=request.expected_cohort,
                    require_complete_jd=request.require_complete_jd,
                    timeout_ms=request.timeout_ms,
                )
            )
            data = audit.data
            accepted_jobs = data.accepted_jobs if data is not None else []
            accepted_ids = {job.id for job in accepted_jobs}
            complete_jd_count = sum(not is_jd_incomplete(job) for job in observed)
            composite_jobs = [
                job for job in accepted_jobs
                if str(job.cohort_source or "").startswith("OC 2027届秋招")
            ]
            cohort_evidence = list(dict.fromkeys(
                str(job.cohort_evidence or "")
                for job in composite_jobs
                if str(job.cohort_evidence or "")
            ))[:5]
            pagination_complete = data.pagination_complete if data is not None else False
            if audit.success:
                integration_status = "connected_complete"
            elif not pagination_complete and raw_jobs:
                integration_status = "connected_partial"
            elif not raw_jobs:
                integration_status, fallback_code, fallback_message = _empty_result_status(
                    source_url,
                    diagnosis,
                    process_result,
                )
            elif data is not None and (
                hydration_candidate_count > 0
            ):
                integration_status = "jd_hydration_required"
            else:
                integration_status = "no_eligible_jobs"
            error_code = process_result.get("error_code") or (
                str(audit.error_code.value) if audit.error_code is not None else None
            )
            error_message = process_result.get("error_message") or audit.error_message
            if not raw_jobs:
                error_code = fallback_code
                error_message = fallback_message
            elif integration_status == "jd_hydration_required" and not process_result.get("error_code"):
                error_code = "jd_hydration_required"
                error_message = "Jobs were observed, but eligible jobs still require complete, identity-bound descriptions."
            elif integration_status == "no_eligible_jobs" and not process_result.get("error_code"):
                error_code = "no_eligible_jobs"
                error_message = "Jobs were observed, but none passed the deterministic eligibility rules."
            if integration_status == "connected_complete":
                approval_state = "awaiting_approval"
            elif integration_status == "invalid_entry":
                approval_state = "rejected"
            elif integration_status == "needs_adapter":
                approval_state = "needs_candidate"
            else:
                approval_state = "isolated_test_failed"
            return OcCandidateCrawlItem(
                company=lead.canonical_name,
                status="succeeded" if audit.success else "failed",
                source_url=original_source_url,
                discovered_entry_url=discovered_entry_url,
                effective_source_urls=process_result.get("effective_source_urls") or [],
                crawler_key=crawler_key,
                integration_status=integration_status,
                raw_job_count=len(raw_jobs),
                accepted_count=data.accepted_count if data is not None else 0,
                rejected_count=data.rejected_count if data is not None else len(raw_jobs),
                complete_jd_count=complete_jd_count,
                incomplete_jd_count=len(observed) - complete_jd_count,
                rejection_reasons=data.rejection_reasons if data is not None else {},
                source_projects=self._project_names(lead),
                job_evidence=(
                    self._job_evidence(observed, accepted_ids)
                    if request.include_job_evidence
                    else []
                ),
                composite_cohort_count=len(composite_jobs),
                cohort_evidence=cohort_evidence,
                pagination_complete=pagination_complete,
                completeness_known=bool(process_result.get("completeness_known", False)),
                pagination_state=data.pagination_state if data is not None else "unknown",
                pages_seen=int(process_result.get("pages_seen") or 0),
                total_pages=process_result.get("total_pages"),
                has_more=bool(process_result.get("has_more", False)),
                advertised_total=process_result.get("advertised_total"),
                termination_reasons=[
                    str(item) for item in process_result.get("termination_reasons") or []
                ][:15] + [
                    f"jd_hydration_{outcome}:{count}"
                    for outcome, count in sorted(hydration_outcomes.items())
                ][:5],
                error_code=error_code,
                error_message=error_message,
                candidate_kind=diagnosis.candidate_kind,
                approval_state=approval_state,
            )
        except subprocess.TimeoutExpired:
            code, message = "timeout", "Candidate crawler exceeded its per-company timeout."
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            code, message = "crawler_failed", str(exc)[-1_000:]
        return OcCandidateCrawlItem(
            company=lead.canonical_name,
            status="failed",
            source_url=original_source_url,
            discovered_entry_url=discovered_entry_url,
            crawler_key=crawler_key,
            integration_status="needs_adapter",
            source_projects=self._project_names(lead),
            error_code=code,
            error_message=message,
            candidate_kind=diagnosis.candidate_kind,
            approval_state="isolated_test_failed",
        )

    def crawl_lead(
        self,
        lead,
        request: OcCandidateCrawlBatchInput,
        *,
        hydrate_details: bool = False,
    ) -> OcCandidateCrawlItem:
        """Evaluate one already-filtered OC lead without writing configuration or jobs."""

        return self._crawl_one(lead, request, hydrate_details=hydrate_details)

    def crawl_public_candidate(
        self,
        company_name: str,
        source_url: str,
        request: OcCandidateCrawlBatchInput,
        *,
        hydrate_details: bool = False,
    ) -> OcCandidateCrawlItem:
        """Crawl one externally discovered URL without consulting a source snapshot."""

        candidate = SourceLead(
            canonical_name=company_name,
            source="bounded_public_search",
            source_urls=(source_url,),
            metadata={
                "entry_discovery_source": "bounded_public_search",
                "recruitment_targets": [str(request.expected_cohort)],
                "recruitment_types": ["秋招"],
            },
        )
        return self._crawl_one(candidate, request, hydrate_details=hydrate_details)

    def crawl(self, request: OcCandidateCrawlBatchInput) -> OcCandidateCrawlBatchResponse:
        started = perf_counter()
        try:
            _, leads = self._new_candidates()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return OcCandidateCrawlBatchResponse(
                tool_name="oc_candidate_crawl_batch",
                status=ToolStatus.FAILURE,
                success=False,
                evidence=[EvidenceSource(source="oc_snapshot", source_ref=str(self.snapshot_path))],
                error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
                error_message=f"OC candidates unavailable: {exc}",
                timeout_ms=request.timeout_ms,
                elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
            )
        if request.company_names:
            by_name = {}
            for lead in leads:
                names = [
                    lead.canonical_name,
                    *(lead.metadata.get("source_project_names") or []),
                ]
                for name in names:
                    by_name[str(name)] = lead
            selected = []
            selected_ids: set[int] = set()
            for name in request.company_names:
                lead = by_name.get(name)
                if lead is not None and id(lead) not in selected_ids:
                    selected.append(lead)
                    selected_ids.add(id(lead))
        else:
            addressable_leads = [
                lead
                for lead in leads
                if any(_is_addressable_candidate_url(url) for url in lead.source_urls)
            ]
            selected = addressable_leads[request.offset : request.offset + request.limit]
        selected = selected[:3]
        results: list[OcCandidateCrawlItem] = []
        with ThreadPoolExecutor(max_workers=max(1, len(selected))) as executor:
            futures = {executor.submit(self.crawl_lead, lead, request): lead for lead in selected}
            for future in as_completed(futures):
                results.append(future.result())
        order = {lead.canonical_name: index for index, lead in enumerate(selected)}
        results.sort(key=lambda item: order.get(item.company, len(order)))
        addressable = sum(item.status != "not_addressable" for item in results)
        succeeded = sum(item.status == "succeeded" for item in results)
        failed = sum(item.status == "failed" for item in results)
        not_addressable = sum(item.status == "not_addressable" for item in results)
        complete = sum(item.integration_status == "connected_complete" for item in results)
        partial = sum(item.integration_status == "connected_partial" for item in results)
        needs_adapter = sum(item.integration_status == "needs_adapter" for item in results)
        invalid_entry = sum(item.integration_status == "invalid_entry" for item in results)
        return OcCandidateCrawlBatchResponse(
            tool_name="oc_candidate_crawl_batch",
            status=ToolStatus.SUCCESS,
            success=True,
            data=OcCandidateCrawlBatchData(
                total_candidates=len(leads),
                selected_count=len(selected),
                addressable_count=addressable,
                attempted_count=addressable,
                succeeded_count=succeeded,
                failed_count=failed,
                not_addressable_count=not_addressable,
                complete_count=complete,
                partial_count=partial,
                needs_adapter_count=needs_adapter,
                invalid_entry_count=invalid_entry,
                results=results,
            ),
            evidence=[EvidenceSource(source="oc_snapshot", source_ref=str(self.snapshot_path))],
            timeout_ms=request.timeout_ms,
            elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
        )


__all__ = [
    "OcAdapterCandidateTestInput",
    "OcAdapterCandidateTestResponse",
    "OcCandidateCrawlBatchInput",
    "OcCandidateCrawlBatchResponse",
    "OcCandidateJobEvidence",
    "OcCandidateListInput",
    "OcCandidateListResponse",
    "OcCandidatePageObserveInput",
    "OcCandidatePageObserveResponse",
    "OcCandidateRunner",
    "SubprocessCandidateCrawlerProcess",
    "OcEntryDiagnosis",
    "diagnose_candidate_entry",
    "infer_candidate_crawler",
]
