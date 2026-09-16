"""Run the frozen entry-discovery evaluation with bounded read-only crawls."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.discovery.public_entries import discover_company_entry_candidates
from packages.recruitment_core.entry import diagnose_candidate_entry


DEFAULT_FIXTURE = ROOT / "evals" / "fixtures" / "entry_discovery_36_20260907.json"
DEFAULT_OUTPUT = ROOT / ".data" / "evals" / "entry_discovery_36_20260907.json"
DEFAULT_WORKERS = 3
MAX_ENTRY_CANDIDATES = 5
STATUSES = (
    "jobs_complete",
    "jobs_partial",
    "activity_empty",
    "access_blocked",
    "entry_not_found",
    "crawl_failed",
)
STATUS_PRIORITY = {
    status: len(STATUSES) - index
    for index, status in enumerate(STATUSES)
}
ENTRY_NOT_FOUND_CODES = {
    "adapter_variant_unsupported",
    "form_application_only",
    "invalid_entry",
    "no_entry_found",
    "recruitment_entry_discovery_required",
    "site_adapter_required",
}
BLOCKED_MARKERS = (
    "access_denied",
    "access blocked",
    "captcha",
    "forbidden",
    "login_required",
    "login required",
    "security verification",
    "验证码",
    "安全验证",
    "401",
    "403",
)

SearchRunner = Callable[..., tuple[list[str], list[Any]]]
CrawlRunner = Callable[..., Mapping[str, Any]]


def crawl_company_result_isolated(
    company: Mapping[str, Any],
    *,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    """Load the production isolated runner lazily so injected tests stay lightweight."""
    from packages.pipeline.isolation import crawl_company_result_isolated as isolated_crawl

    return isolated_crawl(company, timeout_seconds=timeout_seconds)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[-1_000:]


def _candidate_payload(candidate: Any) -> dict[str, Any]:
    """Keep search provenance while leaving room for crawl evidence."""
    if isinstance(candidate, Mapping):
        raw = dict(candidate)
        hit = raw.get("hit")
        if isinstance(hit, Mapping):
            hit_data = dict(hit)
        else:
            hit_data = {}
        get_value = lambda key: raw.get(key, hit_data.get(key))
    else:
        hit = getattr(candidate, "hit", None)
        get_value = lambda key: getattr(candidate, key, None)
        hit_data = {
            key: getattr(hit, key, None)
            for key in ("url", "title", "snippet", "provider", "query")
        }

    return {
        "url": _text(get_value("url") or hit_data.get("url")),
        "title": _text(get_value("title") or hit_data.get("title")),
        "snippet": _text(get_value("snippet") or hit_data.get("snippet")),
        "provider": _text(get_value("provider") or hit_data.get("provider")),
        "query": _text(get_value("query") or hit_data.get("query")),
        "score": get_value("score"),
        "entry_kind": _text(get_value("entry_kind")),
        "crawler_key": _text(get_value("crawler_key")),
        "company_evidence": get_value("company_evidence"),
        "verification_status": "candidate_only",
        "origin": "search_candidate",
    }


def _diagnosis_payload(diagnosis: Any) -> dict[str, Any]:
    return {
        "entry_kind": _text(getattr(diagnosis, "entry_kind", "")),
        "crawler_key": _text(getattr(diagnosis, "crawler_key", "")) or None,
        "reason": _text(getattr(diagnosis, "reason", "")),
    }


def _empty_pagination_evidence() -> dict[str, Any]:
    return {
        "pagination_complete": None,
        "completeness_known": None,
        "pagination_state": "unknown",
        "pages_seen": None,
        "total_pages": None,
        "has_more": None,
        "advertised_total": None,
        "termination_reasons": [],
    }


def _non_negative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _pagination_evidence(result: Mapping[str, Any]) -> dict[str, Any]:
    complete = result.get("pagination_complete")
    complete = complete if type(complete) is bool else None
    known = result.get("completeness_known")
    known = known if type(known) is bool else None
    state = result.get("pagination_state")
    if state not in {"complete", "incomplete", "unknown"}:
        if complete is True:
            state = "complete"
        elif complete is False and known is True:
            state = "incomplete"
        else:
            state = "unknown"
    has_more = result.get("has_more")
    has_more = has_more if type(has_more) is bool else None
    reasons = [
        _text(value)
        for value in list(result.get("termination_reasons") or [])[:20]
        if _text(value)
    ]
    return {
        "pagination_complete": complete,
        "completeness_known": known,
        "pagination_state": state,
        "pages_seen": _non_negative_int(result.get("pages_seen")),
        "total_pages": _non_negative_int(result.get("total_pages")),
        "has_more": has_more,
        "advertised_total": _non_negative_int(result.get("advertised_total")),
        "termination_reasons": reasons,
    }


def _normalize_crawl_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("crawl runner must return a mapping")
    result = dict(value)
    if not isinstance(result.get("jobs"), list):
        raise ValueError("crawl result must contain a jobs list")
    return result


def _blocked(value: Any) -> bool:
    text = _text(value).casefold()
    return any(marker.casefold() in text for marker in BLOCKED_MARKERS)


def _complete_pagination(result: Mapping[str, Any]) -> bool:
    evidence = _pagination_evidence(result)
    return bool(
        evidence["pagination_complete"] is True
        and evidence["completeness_known"] is True
        and evidence["has_more"] is not True
        and not result.get("failures")
        and not _text(result.get("error_code"))
    )


def _activity_empty(result: Mapping[str, Any]) -> bool:
    jobs = result.get("jobs") or []
    if jobs:
        return False
    code = _text(result.get("error_code")).casefold()
    evidence = _pagination_evidence(result)
    return bool(
        evidence["pagination_complete"] is True
        and evidence["completeness_known"] is True
        and evidence["has_more"] is False
        and code in {"", "activity_empty", "no_results"}
        and _text(result.get("run_reason")).casefold() in {"", "activity_empty"}
    )


def _classify_result(
    result: Mapping[str, Any],
    *,
    entry_kind: str = "",
) -> str:
    jobs = result.get("jobs") or []
    code = _text(result.get("error_code")).casefold()
    combined_error = " ".join(
        [code, *(_text(value) for value in result.get("failures") or [])]
    )
    if jobs:
        return "jobs_complete" if _complete_pagination(result) else "jobs_partial"
    if _blocked(combined_error) or _blocked(result.get("error_message")):
        return "access_blocked"
    if _activity_empty(result):
        return "activity_empty"
    if code in ENTRY_NOT_FOUND_CODES or entry_kind in {"entry_discovery_required", "form_application"}:
        return "entry_not_found"
    return "crawl_failed"


def _exception_status(exc: BaseException) -> str:
    return "access_blocked" if _blocked(_error_text(exc)) else "crawl_failed"


def _attempt_record(
    spec: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    jobs = result.get("jobs") or []
    status = _classify_result(result, entry_kind=_text(spec.get("entry_kind")))
    error_code = _text(result.get("error_code")) or None
    error_message = _text(result.get("error_message") or result.get("error")) or None
    return {
        **dict(spec.get("metadata") or {}),
        "url": _text(spec.get("url")),
        "crawler_key": _text(result.get("crawler_key")) or _text(spec.get("crawler_key")) or None,
        "origin": _text(spec.get("origin")),
        "verification_status": "verified",
        "attempted": True,
        "status": status,
        "job_count": len(jobs),
        "raw_job_count": _non_negative_int(result.get("raw_job_count")) or len(jobs),
        "pagination_evidence": _pagination_evidence(result),
        "error_code": error_code,
        "error": error_message,
        "result_source_url": _text(result.get("source_url")) or None,
        "crawl_source_url": _text(result.get("crawl_source_url")) or None,
        "discovered_entry_url": _text(result.get("discovered_entry_url")) or None,
        "effective_source_urls": [
            _text(value)
            for value in list(result.get("effective_source_urls") or [])[:20]
            if _text(value)
        ],
        "source_runs": list(result.get("source_runs") or [])[:20],
    }


def _failed_attempt_record(
    spec: Mapping[str, Any],
    exc: BaseException,
) -> dict[str, Any]:
    message = _error_text(exc)
    return {
        **dict(spec.get("metadata") or {}),
        "url": _text(spec.get("url")),
        "crawler_key": _text(spec.get("crawler_key")) or None,
        "origin": _text(spec.get("origin")),
        "verification_status": "failed",
        "attempted": True,
        "status": _exception_status(exc),
        "job_count": 0,
        "raw_job_count": 0,
        "pagination_evidence": _empty_pagination_evidence(),
        "error_code": "timeout" if isinstance(exc, TimeoutError) else type(exc).__name__.casefold(),
        "error": message,
        "result_source_url": None,
        "crawl_source_url": None,
        "discovered_entry_url": None,
        "effective_source_urls": [],
        "source_runs": [],
    }


def _not_attempted_record(
    spec: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    return {
        **dict(spec.get("metadata") or {}),
        "url": _text(spec.get("url")),
        "crawler_key": _text(spec.get("crawler_key")) or None,
        "origin": _text(spec.get("origin")),
        "verification_status": "not_attempted",
        "attempted": False,
        "status": "crawl_failed",
        "job_count": None,
        "raw_job_count": None,
        "pagination_evidence": _empty_pagination_evidence(),
        "error_code": "timeout",
        "error": reason,
        "result_source_url": None,
        "crawl_source_url": None,
        "discovered_entry_url": None,
        "effective_source_urls": [],
        "source_runs": [],
    }


def _crawl_payload(company: Mapping[str, Any], url: str, crawler_key: str) -> dict[str, Any]:
    payload = dict(company)
    payload.update({
        "careers_url": url,
        "crawler": crawler_key,
        "campaign_url": "",
        "campaign_urls": [],
        "link_kind": "",
    })
    return payload


def _base_company_result(company: Mapping[str, Any], diagnosis: Any) -> dict[str, Any]:
    url = _text(company.get("careers_url") or company.get("url"))
    return {
        "id": company.get("id"),
        "name": company.get("name"),
        "cohort": company.get("cohort"),
        "industries": list(company.get("industries") or []),
        "current_url": url,
        "current_diagnosis": _diagnosis_payload(diagnosis),
        "candidates": [],
        "crawl_attempts": [],
        "queries": [],
        "candidate_urls": [],
        "error": None,
        "search_error": None,
        "read_only": True,
    }


def _fallback_diagnosis(url: str) -> Any:
    try:
        return diagnose_candidate_entry(url)
    except Exception:  # noqa: BLE001 - a malformed fixture is a company-local failure
        return type("Diagnosis", (), {
            "entry_kind": "invalid_entry",
            "crawler_key": None,
            "reason": "diagnosis_failed",
        })()


def _unexpected_company_result(company: Mapping[str, Any], exc: BaseException) -> dict[str, Any]:
    diagnosis = _fallback_diagnosis(_text(company.get("careers_url") or company.get("url")))
    result = _base_company_result(company, diagnosis)
    spec = {
        "url": result["current_url"],
        "crawler_key": _text(company.get("crawler")) or _text(getattr(diagnosis, "crawler_key", "")) or "render",
        "origin": "current_url",
        "entry_kind": _text(getattr(diagnosis, "entry_kind", "")),
        "metadata": {},
    }
    record = _failed_attempt_record(spec, exc)
    result.update({
        "action": "validate_existing_url",
        "status": "crawl_failed",
        "crawl_attempts": [record],
        "candidate_urls": [],
        "crawler_key": record["crawler_key"],
        "job_count": 0,
        "raw_job_count": 0,
        "pagination_evidence": record["pagination_evidence"],
        "error": record["error"],
        "error_code": record["error_code"],
        "elapsed_ms": 0,
    })
    return result


def _aggregate_status(records: list[Mapping[str, Any]], search_error: str | None) -> str:
    statuses = [
        _text(record.get("status"))
        for record in records
        if _text(record.get("status")) in STATUSES
    ]
    if not statuses:
        return "crawl_failed"
    best = max(statuses, key=lambda value: STATUS_PRIORITY[value])
    if search_error and best in {"entry_not_found", "crawl_failed"}:
        return "crawl_failed"
    return best


def evaluate_company(
    company: dict[str, Any],
    *,
    search: SearchRunner | None = None,
    crawl: CrawlRunner | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Evaluate one company without mutating its fixture or project state."""
    started = perf_counter()
    search_runner = search or discover_company_entry_candidates
    crawl_runner = crawl or crawl_company_result_isolated
    deadline = started + max(0.0, timeout_seconds)
    url = _text(company.get("careers_url") or company.get("url"))
    diagnosis = _fallback_diagnosis(url)
    result = _base_company_result(company, diagnosis)

    if getattr(diagnosis, "entry_kind", "") == "form_application":
        action = "excluded_form"
    elif getattr(diagnosis, "entry_kind", "") == "entry_discovery_required":
        action = "search_candidates"
    else:
        action = "validate_existing_url"
    result["action"] = action

    queries: list[str] = []
    candidates: list[Any] = []
    search_error: str | None = None
    if action == "search_candidates":
        remaining = deadline - perf_counter()
        if remaining <= 0:
            search_error = "TimeoutError: entry evaluation deadline exhausted."
        else:
            try:
                queries, candidates = search_runner(
                    _text(company.get("name")),
                    timeout_seconds=remaining,
                    max_queries=2,
                    max_candidates=MAX_ENTRY_CANDIDATES,
                )
                queries = [_text(query) for query in list(queries or []) if _text(query)]
                candidates = list(candidates or [])[:MAX_ENTRY_CANDIDATES]
            except Exception as exc:  # noqa: BLE001 - one provider cannot stop the batch
                search_error = _error_text(exc)

    candidate_metadata: list[dict[str, Any]] = []
    seen_urls = {url}
    for candidate in candidates:
        metadata = _candidate_payload(candidate)
        candidate_url = _text(metadata.get("url"))
        if not candidate_url or candidate_url in seen_urls:
            continue
        seen_urls.add(candidate_url)
        candidate_diagnosis = _fallback_diagnosis(candidate_url)
        metadata["crawler_key"] = (
            _text(metadata.get("crawler_key"))
            or _text(getattr(candidate_diagnosis, "crawler_key", ""))
            or "render"
        )
        metadata["entry_kind"] = _text(metadata.get("entry_kind")) or _text(
            getattr(candidate_diagnosis, "entry_kind", "")
        )
        candidate_metadata.append(metadata)

    specs: list[dict[str, Any]] = []
    if action == "validate_existing_url":
        specs.append({
            "url": url,
            "crawler_key": _text(company.get("crawler"))
            or _text(getattr(diagnosis, "crawler_key", ""))
            or "render",
            "origin": "current_url",
            "entry_kind": _text(getattr(diagnosis, "entry_kind", "")),
            "metadata": {},
        })
    for metadata in candidate_metadata:
        specs.append({
            "url": metadata["url"],
            "crawler_key": metadata["crawler_key"],
            "origin": "search_candidate",
            "entry_kind": metadata.get("entry_kind") or "",
            "metadata": metadata,
        })

    attempts: list[dict[str, Any]] = []
    if action == "excluded_form":
        attempts.append({
            "url": url,
            "crawler_key": _text(company.get("crawler"))
            or _text(getattr(diagnosis, "crawler_key", ""))
            or None,
            "origin": "current_url",
            "verification_status": "not_attempted",
            "attempted": False,
            "status": "entry_not_found",
            "job_count": 0,
            "raw_job_count": 0,
            "pagination_evidence": _empty_pagination_evidence(),
            "error_code": "form_application_only",
            "error": _text(getattr(diagnosis, "reason", "form application")),
            "result_source_url": None,
            "crawl_source_url": None,
            "discovered_entry_url": None,
            "effective_source_urls": [],
            "source_runs": [],
        })

    for spec in specs:
        remaining = deadline - perf_counter()
        if remaining <= 0:
            attempts.append(_not_attempted_record(
                spec, reason="TimeoutError: entry evaluation deadline exhausted."
            ))
            continue
        try:
            raw_result = crawl_runner(
                _crawl_payload(company, _text(spec["url"]), _text(spec["crawler_key"])),
                timeout_seconds=remaining,
            )
            attempts.append(_attempt_record(spec, _normalize_crawl_result(raw_result)))
        except Exception as exc:  # noqa: BLE001 - isolate one URL/company failure
            attempts.append(_failed_attempt_record(spec, exc))

    candidate_attempts = [
        attempt for attempt in attempts if attempt.get("origin") == "search_candidate"
    ]
    if action == "search_candidates" and not candidate_metadata and not search_error:
        status = "entry_not_found"
    else:
        status = _aggregate_status(attempts, search_error)
    selected = next(
        (attempt for attempt in attempts if attempt.get("status") == status),
        attempts[0] if attempts else None,
    )
    selected = selected or {
        "url": url,
        "crawler_key": None,
        "job_count": 0,
        "raw_job_count": 0,
        "pagination_evidence": _empty_pagination_evidence(),
        "error_code": "entry_not_found" if status == "entry_not_found" else None,
        "error": None,
        "crawl_source_url": None,
        "discovered_entry_url": None,
    }
    errors = [
        value for value in [search_error, *(attempt.get("error") for attempt in attempts)]
        if _text(value)
    ]
    result.update({
        "status": status,
        "queries": queries,
        "candidates": candidate_attempts,
        "crawl_attempts": attempts,
        "candidate_urls": [metadata["url"] for metadata in candidate_metadata],
        "crawler_key": selected.get("crawler_key"),
        "job_count": selected.get("job_count") if selected.get("job_count") is not None else 0,
        "raw_job_count": selected.get("raw_job_count") if selected.get("raw_job_count") is not None else 0,
        "pagination_evidence": selected.get("pagination_evidence") or _empty_pagination_evidence(),
        "crawl_source_url": selected.get("crawl_source_url") or (
            selected.get("url") if selected.get("attempted") else None
        ),
        "discovered_entry_url": selected.get("discovered_entry_url"),
        "error_code": selected.get("error_code") or ("search_failed" if search_error else None),
        "error": errors[0] if errors else None,
        "search_error": search_error,
        "elapsed_ms": max(0, int((perf_counter() - started) * 1_000)),
    })
    return result


def _safe_evaluate(
    index: int,
    company: dict[str, Any],
    *,
    search: SearchRunner | None,
    crawl: CrawlRunner | None,
    timeout_seconds: float,
) -> tuple[int, dict[str, Any]]:
    try:
        return index, evaluate_company(
            company,
            search=search,
            crawl=crawl,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - retain one company's failure in the report
        return index, _unexpected_company_result(company, exc)


def run_evaluation(
    fixture: Path,
    *,
    cohort: str = "all",
    limit: int | None = None,
    search: SearchRunner | None = None,
    timeout_seconds: float = 30.0,
    crawl: CrawlRunner | None = None,
    workers: int = DEFAULT_WORKERS,
    concurrency: int | None = None,
    max_workers: int | None = None,
) -> dict[str, Any]:
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    companies = list(payload.get("companies") or [])
    if cohort != "all":
        companies = [item for item in companies if item.get("cohort") == cohort]
    if limit is not None:
        companies = companies[:limit]
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if concurrency is not None:
        workers = concurrency
    if max_workers is not None:
        workers = max_workers
    if workers < 1:
        raise ValueError("workers must be positive")

    ordered_results: list[dict[str, Any] | None] = [None] * len(companies)
    if companies:
        with ThreadPoolExecutor(max_workers=min(workers, len(companies))) as executor:
            futures = {
                executor.submit(
                    _safe_evaluate,
                    index,
                    company,
                    search=search,
                    crawl=crawl,
                    timeout_seconds=timeout_seconds,
                ): index
                for index, company in enumerate(companies)
            }
            for future in as_completed(futures):
                index, result = future.result()
                ordered_results[index] = result
    results = [result for result in ordered_results if result is not None]
    actions = Counter(item["action"] for item in results)
    status_counts = Counter(item["status"] for item in results)
    statuses = {status: status_counts.get(status, 0) for status in STATUSES}
    return {
        "schema_version": "entry-discovery-eval-v2",
        "fixture": str(fixture.resolve()),
        "fixture_version": payload.get("fixture_version"),
        "cohort": cohort,
        "workers": min(workers, len(companies)) if companies else 0,
        "timeout_seconds": timeout_seconds,
        "read_only": True,
        "model_calls": 0,
        "writes": {"config": 0, "database": 0},
        "summary": {
            "selected": len(results),
            "actions": dict(sorted(actions.items())),
            "statuses": statuses,
            "candidate_count": sum(len(item["candidates"]) for item in results),
            "crawl_attempt_count": sum(
                sum(bool(attempt.get("attempted")) for attempt in item["crawl_attempts"])
                for item in results
            ),
            "search_error_count": sum(bool(item["search_error"]) for item in results),
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--cohort", choices=("debug", "acceptance", "all"), default="all")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--workers", "--concurrency", dest="workers", type=int, default=DEFAULT_WORKERS,
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.workers < 1:
        parser.error("--workers must be positive")
    report = run_evaluation(
        args.fixture,
        cohort=args.cohort,
        limit=args.limit,
        timeout_seconds=args.timeout_seconds,
        workers=args.workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output.resolve()),
        "read_only": report["read_only"],
        **report["summary"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
