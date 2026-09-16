"""Standalone entry points for running one configured company crawler."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .crawlers import CRAWLER_MAP
from . import job_cohorts, job_filters
from .models import CompanyConfig

logger = logging.getLogger(__name__)

_IDENTITY_CRAWLERS = {
    "alibaba", "beisen", "beisen_mobile", "feishu", "hotjob", "moka", "moseeker",
}


def _source_identity(url: str, crawler: str) -> str:
    """Keep the same platform-level URL identity used by the legacy runner."""
    parsed = urlsplit(url)
    host = parsed.netloc.casefold()
    path = parsed.path.rstrip("/")
    key = crawler.casefold()
    if not host:
        return ""
    if key == "moka":
        match = re.search(r"/(?:campus_apply|campus-recruitment)/([^/?#]+)", path, re.I)
        marker = match.group(1).casefold() if match else ""
        return f"moka:{marker or host + ':' + path.casefold()}"
    if key == "hotjob":
        match = re.search(r"/(SU[0-9a-f]+)", path, re.I)
        marker = match.group(1).casefold() if match else ""
        return f"hotjob:{marker or host + ':' + path.casefold()}"
    if key == "feishu":
        return f"feishu:{host}"
    if key in {"beisen", "beisen_mobile"}:
        query = parse_qs(parsed.query)
        department = (query.get("p") or query.get("department") or [""])[0]
        return f"{key}:{host}:{path.casefold() or '/campus/jobs'}:{department}"
    if key == "moseeker":
        match = re.search(r"/positions/index/cid/(\d+)", path, re.I)
        return f"moseeker:{match.group(1)}" if match else f"moseeker:{host}:{path.casefold()}"
    if key == "alibaba":
        query = parse_qs(parsed.query)
        batch_id = (query.get("batchId") or [""])[0]
        raw_filters = (query.get("filterParams") or [""])[0]
        try:
            filters = json.dumps(
                json.loads(raw_filters),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) if raw_filters else ""
        except (TypeError, ValueError, json.JSONDecodeError):
            filters = raw_filters
        return f"alibaba:{host}:{batch_id}:{filters}" if filters else f"alibaba:{host}:{batch_id}"
    return f"{key}:{host}"


def configured_crawl_urls(company: CompanyConfig | Mapping[str, Any]) -> list[str]:
    """Return the base URL and distinct compatible campaign URLs."""
    config = CompanyConfig.from_legacy(company)
    candidates = [config.careers_url, *config.campaign_urls]
    if config.crawler == "alibaba" and any(
        parse_qs(urlsplit(url).query).get("batchId") for url in config.campaign_urls
    ):
        candidates = list(config.campaign_urls)

    urls: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if not url:
            continue
        parsed = urlsplit(url)
        identity = (
            _source_identity(url, config.crawler)
            if config.crawler in _IDENTITY_CRAWLERS
            else urlunsplit((
                parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path.rstrip("/"),
                parsed.query, "",
            ))
        )
        if identity in seen:
            continue
        seen.add(identity)
        urls.append(url)
    return urls


def _normalize_configured_listing_links(config: CompanyConfig, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if config.link_kind != "list":
        return jobs
    parts = urlsplit(config.careers_url)
    for job in jobs:
        identity = hashlib.sha1(
            f"{job.get('title', '')}|{job.get('city', '')}|{job.get('jd_url', '')}".encode("utf-8")
        ).hexdigest()[:12]
        marker = f"job-ref={identity}"
        if parts.fragment:
            separator = "&" if "?" in parts.fragment else "?"
            fragment = f"{parts.fragment}{separator}{marker}"
        else:
            fragment = marker
        job["jd_url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, fragment))
        job["link_kind"] = "list"
    return jobs


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _observation_key(job: Mapping[str, Any]) -> str:
    detail_url = str(job.get("jd_url") or "").strip()
    title_city = "|".join(
        (str(job.get("title") or "").strip(), str(job.get("city") or "").strip())
    )
    if str(job.get("link_kind") or "").casefold() == "list":
        return f"list:{detail_url}:{title_city}"
    return detail_url or title_city


def _crawler_evidence(crawler: Any, observed_count: int, source_url: str) -> dict[str, Any]:
    """Read adapter-owned completeness signals without inventing pagination facts."""

    explicit_complete = getattr(crawler, "pagination_complete", None)
    if explicit_complete is not None:
        explicit_complete = bool(explicit_complete)
    pages_seen = None
    for name in ("pages_seen", "pages_fetched", "page_count"):
        pages_seen = _optional_int(getattr(crawler, name, None))
        if pages_seen is not None:
            break
    total_pages = None
    for name in ("total_pages", "expected_pages"):
        total_pages = _optional_int(getattr(crawler, name, None))
        if total_pages is not None:
            break
    advertised_total = None
    for name in ("advertised_total", "expected_total", "total_count"):
        advertised_total = _optional_int(getattr(crawler, name, None))
        if advertised_total is not None:
            break
    has_more = bool(getattr(crawler, "has_more", False))
    reason = str(getattr(crawler, "pagination_termination_reason", "") or "").strip()
    if explicit_complete is None:
        reason = reason or "adapter_did_not_report_completeness"
    elif explicit_complete:
        reason = reason or "adapter_reported_complete"
    else:
        reason = reason or "adapter_reported_incomplete"
    return {
        "source_url": source_url,
        "effective_source_url": str(
            getattr(crawler, "resolved_source_url", "") or source_url
        ),
        "pagination_complete": explicit_complete,
        "pages_seen": pages_seen,
        "total_pages": total_pages,
        "has_more": has_more,
        "advertised_total": advertised_total,
        "observed_total": observed_count,
        "termination_reason": reason,
        "fetch_failed": bool(getattr(crawler, "fetch_failed", False)),
        "error_code": str(getattr(crawler, "crawl_error_code", "") or ""),
        "project_diagnostics": [
            {key: item[key] for key in (
                "project_id", "pagination_complete", "pages_seen", "total_pages",
                "advertised_total", "observed_unique", "has_more", "termination_reason",
                "fetch_failed", "scope_changed", "old_project_id", "observed_project_id",
                "observed_project_name",
            ) if key in item}
            for item in (getattr(crawler, "project_diagnostics", None) or [])[:20]
            if isinstance(item, dict)
        ],
        "pagination_diagnostics": [
            {key: item[key] for key in ("page", "count", "total", "reason", "changed", "scope_hash") if key in item}
            for item in (getattr(crawler, "pagination_diagnostics", None) or [])[:30]
            if isinstance(item, dict)
        ],
    }


def crawl_company_with_evidence(
    company: CompanyConfig | Mapping[str, Any],
    *,
    crawler_map: Mapping[str, type] | None = None,
) -> dict[str, Any]:
    """Run one company and retain adapter-owned completeness evidence."""
    config = CompanyConfig.from_legacy(company)
    registry = CRAWLER_MAP if crawler_map is None else crawler_map
    crawler_class = registry.get(config.crawler)
    if crawler_class is None:
        raise KeyError(f"unknown crawler key: {config.crawler}")

    jobs: list[dict[str, Any]] = []
    failures: list[str] = []
    source_runs: list[dict[str, Any]] = []
    for crawl_url in configured_crawl_urls(config):
        crawler = crawler_class(config.name, crawl_url)
        batch_jobs = crawler.fetch()
        evidence = _crawler_evidence(crawler, len(batch_jobs), crawl_url)
        unique_observed = len({_observation_key(job) for job in batch_jobs})
        evidence["unique_observed_total"] = unique_observed
        if evidence["pagination_complete"] is True and (
            evidence["has_more"]
            or (evidence["pages_seen"] is not None and evidence["total_pages"] is not None
                and evidence["pages_seen"] < evidence["total_pages"])
            or (evidence["advertised_total"] is not None
                and unique_observed != evidence["advertised_total"])
        ):
            evidence["pagination_complete"] = False
            evidence["termination_reason"] = "source_evidence_conflict"
        source_runs.append(evidence)
        if evidence["pagination_complete"] is False:
            reason = str(evidence["termination_reason"] or "unknown")
            logger.error(
                "[%s] pagination incomplete (%s); retained %d observations for audit from %s",
                config.name, reason, len(batch_jobs), crawl_url,
            )
            failures.append(reason)
        if not batch_jobs and evidence["fetch_failed"]:
            failures.append("render_failed")
        jobs.extend(batch_jobs)

    deduped: list[dict[str, Any]] = []
    seen_jobs: set[str] = set()
    for job in jobs:
        identity = _observation_key(job)
        if identity in seen_jobs:
            continue
        seen_jobs.add(identity)
        deduped.append(job)

    if config.campaign_text:
        for job in deduped:
            if not str(job.get("campaign_text") or "").strip():
                job["campaign_text"] = config.campaign_text
    entry_click_texts = config.extra.get("entry_click_texts")
    if isinstance(entry_click_texts, (list, tuple)):
        for job in deduped:
            job.setdefault("entry_click_texts", list(entry_click_texts))
    detail_interaction = config.extra.get("detail_interaction")
    if isinstance(detail_interaction, Mapping):
        for job in deduped:
            job.setdefault("detail_interaction", dict(detail_interaction))
    for job in deduped:
        job["recruitment_track"] = job_filters.recruitment_track(job)
    config_payload = config.to_dict()
    effective_campaign_url = next(
        (
            str(run.get("effective_source_url") or "")
            for run in source_runs
            if str(run.get("effective_source_url") or "").startswith(("http://", "https://"))
        ),
        config.campaign_url or config.careers_url,
    )
    trusted_campaign = job_cohorts.trusted_source_campaign(
        config_payload,
        jobs_observed=bool(deduped),
    )
    inspected_campaign = None
    if (
        trusted_campaign is None
        and config_payload.get("source_cohort_source") == job_cohorts.OC_TRUSTED_SOURCE
    ):
        inspected_campaign = job_cohorts.inspect_official_campaign(
            effective_campaign_url
        )
        trusted_campaign = job_cohorts.trusted_source_campaign(
            config_payload,
            inspected_campaign,
            jobs_observed=bool(deduped),
        )
    classified = job_cohorts.annotate_company_jobs(
        deduped,
        effective_campaign_url,
        inspect_page=trusted_campaign is None and inspected_campaign is None,
        campaign=trusted_campaign or inspected_campaign,
    )
    classified = _normalize_configured_listing_links(config, classified)
    explicit_states = [run["pagination_complete"] for run in source_runs]
    pagination_complete = bool(source_runs) and all(state is True for state in explicit_states)
    pages = [run["pages_seen"] for run in source_runs]
    totals = [run["total_pages"] for run in source_runs]
    advertised = [run["advertised_total"] for run in source_runs]
    # Each source is audited before unioning overlapping campaigns. Summing their
    # totals after deduplication would manufacture a missing-jobs failure.
    overlapping_sources = len(source_runs) > 1 and len(deduped) < len(jobs)
    errors = [run["error_code"] for run in source_runs if run.get("error_code")]
    return {
        "jobs": classified,
        "raw_job_count": len(classified),
        "pagination_complete": pagination_complete,
        "completeness_known": bool(source_runs) and all(state is not None for state in explicit_states),
        "pagination_state": (
            "complete" if pagination_complete else
            "incomplete" if any(state is False for state in explicit_states) else "unknown"
        ),
        "pages_seen": sum(int(value) for value in pages if value is not None),
        "total_pages": (
            sum(int(value) for value in totals if value is not None)
            if totals and all(value is not None for value in totals)
            else None
        ),
        "has_more": any(bool(run["has_more"]) for run in source_runs),
        "advertised_total": (
            sum(int(value) for value in advertised if value is not None)
            if advertised and all(value is not None for value in advertised) and not overlapping_sources
            else None
        ),
        "advertised_total_scope": "per_source" if overlapping_sources else "aggregate",
        "error_code": errors[0] if errors else None,
        "termination_reasons": list(dict.fromkeys(
            str(run["termination_reason"]) for run in source_runs if run["termination_reason"]
        )),
        "source_runs": source_runs,
        "failures": failures,
        "effective_source_urls": list(dict.fromkeys(
            str(run["effective_source_url"])
            for run in source_runs
            if str(run.get("effective_source_url") or "")
        )),
    }


def crawl_company(
    company: CompanyConfig | Mapping[str, Any],
    *,
    crawler_map: Mapping[str, type] | None = None,
) -> list[dict[str, Any]]:
    """Compatibility entry point; explicit incomplete adapters still return no rows."""

    result = crawl_company_with_evidence(company, crawler_map=crawler_map)
    source_runs = result.get("source_runs") or []
    if any(run.get("pagination_complete") is False for run in source_runs):
        return []
    return list(result["jobs"])


__all__ = [
    "CompanyConfig",
    "configured_crawl_urls",
    "crawl_company",
    "crawl_company_with_evidence",
]
