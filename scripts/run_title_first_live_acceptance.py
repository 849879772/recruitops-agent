"""Run a bounded, read-only live detail pilot and an isolated DB acceptance."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from threading import Lock
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.discovery.company_registry import (  # noqa: E402
    CompanySourceAttempt,
    CompanySourceRecord,
    CompanySourceRegistry,
)
from packages.domain.models import ApplicationStage, Company, Job, JobAnalysis, RecruitmentBatch  # noqa: E402
from packages.matching.title_policy import normalize_job_title_key, screen_title_job  # noqa: E402
from packages.pipeline.isolation import (  # noqa: E402
    IsolatedOperationTimeout,
    IsolatedWorkerError,
    fetch_job_detail_result_isolated,
)
from packages.recruitment_core.jd_capture import assess_jd_capture  # noqa: E402
from packages.recruitment_core.jd_repair import content_sha256, validate_candidate  # noqa: E402
from packages.recruitment_core.offerbiu_policy import apply_offerbiu_cohort  # noqa: E402
from packages.storage import (  # noqa: E402
    ApplicationSnapshot,
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
    Storage,
)
from packages.storage.sync import upsert_company_snapshot, upsert_job_snapshot  # noqa: E402


SOURCE_NAMES = ("existing-repairs", "new-failures", "missing-jd")
EXPECTED_SOURCE_COUNTS = {"existing-repairs": 14, "new-failures": 892, "missing-jd": 1404}
ACCEPTANCE_SOURCE = "title_first_live_acceptance_20260909"
SCHEMA_VERSION = "title-first-live-acceptance.v1"
UTC = timezone.utc
DOMAIN_BARRIER_STATUSES = frozenset({"captcha_required", "login_required", "access_blocked"})


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: object) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()


def _host(url: object) -> str:
    return (urlsplit(str(url or "")).hostname or "").casefold()


def platform_family(url: object) -> str:
    """Map a detail host to the adapter/platform family used by the runner."""

    host = _host(url)
    if host.endswith(".zhiye.com"):
        return "beisen"
    if host == "jobs.feishu.cn" or host.endswith(".jobs.feishu.cn") or host.endswith(".mioffice.cn"):
        return "feishu"
    if host == "app.mokahr.com" or host.endswith(".mokahr.com"):
        return "moka"
    if host == "career.huawei.com":
        return "huawei"
    if host == "join.qq.com":
        return "tencent"
    if host == "campus.jd.com":
        return "jd"
    if host == "jobs.51job.com" or host.endswith(".51job.com"):
        return "51job"
    if host == "xiaoyuan.zhaopin.com" or host.endswith(".zhaopin.com"):
        return "zhaopin"
    if host == "hotjob.cn" or host.endswith(".hotjob.cn"):
        return "hotjob"
    return "custom_render"


def _observation(row: Mapping[str, Any]) -> Mapping[str, Any]:
    target = str(row.get("detail_url") or "")
    observations = row.get("observations") or []
    for item in observations:
        raw = item.get("raw_job") or {}
        if str(raw.get("jd_url") or raw.get("detail_url") or "") == target:
            return item
    if observations:
        return observations[0]
    return {"raw_job": {}, "source_url": ""}


def _build_input_job(
    row: Mapping[str, Any], *, existing: bool, company_recipe: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    observation = _observation(row)
    raw = dict(observation.get("raw_job") or {})
    stored = {}
    if existing:
        stored = dict(row.get("repair_target_job") or {})
        if not stored and row.get("existing_jobs"):
            stored = dict((row["existing_jobs"][0] or {}).get("job") or {})
    job = {**stored, **raw}
    detail_url = str(row.get("detail_url") or job.get("jd_url") or job.get("detail_url") or "").strip()
    source_url = str(observation.get("source_url") or row.get("source_urls", [""])[0] or "").strip()
    job.update(
        {
            "company": row.get("company_name") or row.get("company") or job.get("company"),
            "company_name": row.get("company_name") or row.get("company") or job.get("company"),
            "company_id": row.get("company_id"),
            "title": row.get("title") or job.get("title"),
            "detail_url": detail_url,
            "jd_url": detail_url,
            "careers_url": source_url,
            "company_campus_url": source_url,
            "source": "offerbiu_snapshot",
            "discovery_source": "offerbiu",
            "source_platform": platform_family(detail_url),
            "batch": "formal",
            "detail_capture_policy": "title_first_v2",
            "capture_evidence": {},
        }
    )
    if company_recipe:
        # Frozen rows predate current company interaction recipes. Reapply the
        # configured recipe so this acceptance exercises the production path.
        for key in ("entry_click_texts", "detail_interaction"):
            if key in company_recipe and company_recipe[key] is not None:
                job.setdefault(key, deepcopy(company_recipe[key]))
    if existing:
        existing_ids = list(row.get("existing_job_ids") or [])
        if not existing_ids:
            raise ValueError(f"Existing repair row has no job ID: {row.get('title')}")
        job["id"] = existing_ids[0]
    return job


def _screening_dump(screening: Any) -> dict[str, Any]:
    if hasattr(screening, "model_dump"):
        return screening.model_dump(mode="json")
    return {
        "eligible": bool(getattr(screening, "eligible", False)),
        "analysis_status": str(getattr(getattr(screening, "analysis_status", None), "value", "")),
        "reasons": [str(getattr(item, "value", item)) for item in getattr(screening, "reasons", [])],
    }


def _diverse_pick(rows: list[dict[str, Any]], limit: int, *, prefer_platform: bool) -> list[dict[str, Any]]:
    remaining = sorted(
        rows,
        key=lambda row: (
            str(row.get("company_id") or ""),
            platform_family(row.get("detail_url")),
            str(row.get("title") or ""),
            str(row.get("detail_url") or ""),
        ),
    )
    chosen: list[dict[str, Any]] = []
    companies: set[str] = set()
    platforms: set[str] = set()
    while remaining and len(chosen) < limit:
        def score(row: Mapping[str, Any]) -> tuple[int, int, str, str, str]:
            company = str(row.get("company_id") or "")
            platform = platform_family(row.get("detail_url"))
            first = int(platform not in platforms) if prefer_platform else int(company not in companies)
            second = int(company not in companies) if prefer_platform else int(platform not in platforms)
            return first, second, company, platform, str(row.get("title") or "")

        selected = max(remaining, key=score)
        remaining.remove(selected)
        chosen.append(selected)
        companies.add(str(selected.get("company_id") or ""))
        platforms.add(platform_family(selected.get("detail_url")))
    return chosen


def _sample_record(
    index: int,
    stratum: str,
    row: dict[str, Any],
    *,
    existing: bool,
    profile: Any,
    company_recipe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    input_job = _build_input_job(row, existing=existing, company_recipe=company_recipe)
    screening = screen_title_job(input_job, profile)
    if not screening.eligible:
        raise ValueError(
            f"Frozen title unexpectedly failed title-first screening: {row.get('company_name')} / {row.get('title')}"
        )
    request_job = apply_offerbiu_cohort(input_job)
    job_id = (
        str(request_job["id"])
        if existing
        else "title-first-" + sha256(
            f"{row.get('company_id')}\0{normalize_job_title_key(row.get('title'))}\0{row.get('detail_url')}".encode("utf-8")
        ).hexdigest()
    )
    request_job["id"] = job_id
    return {
        "index": index,
        "stratum": stratum,
        "platform": platform_family(row.get("detail_url")),
        "historical_failure_reason": row.get("capture_failure_reason"),
        "company_id": row.get("company_id"),
        "company": row.get("company_name"),
        "title": row.get("title"),
        "detail_url": row.get("detail_url"),
        "source_urls": list(row.get("source_urls") or []),
        "existing_job_ids": list(row.get("existing_job_ids") or []),
        "job_id": job_id,
        "title_screening": _screening_dump(screening),
        "input_job": input_job,
        "request_job": request_job,
    }


def _load_company_recipes() -> dict[str, Mapping[str, Any]]:
    config = yaml.safe_load((ROOT / "config" / "companies.yaml").read_text(encoding="utf-8")) or {}
    recipes: dict[str, Mapping[str, Any]] = {}
    for company in config.get("companies", []):
        if not isinstance(company, Mapping):
            continue
        recipe = {
            key: deepcopy(company[key])
            for key in ("entry_click_texts", "detail_interaction")
            if key in company
        }
        if not recipe:
            continue
        for key in (company.get("id"), company.get("name")):
            if key:
                recipes[str(key)] = recipe
    return recipes


def prepare(
    source: Path,
    output: Path,
    profile: Any,
    *,
    full: bool = False,
    include_existing: bool = True,
) -> dict[str, Any]:
    rows_by_name = {name: _read_jsonl(source / f"{name}.jsonl") for name in SOURCE_NAMES}
    actual_counts = {name: len(rows) for name, rows in rows_by_name.items()}
    if actual_counts != EXPECTED_SOURCE_COUNTS:
        raise ValueError(f"Frozen source counts changed: {actual_counts}")

    selected: list[tuple[str, dict[str, Any], bool]] = []
    if include_existing:
        for row in rows_by_name["existing-repairs"]:
            selected.append(("existing_empty", row, True))

    failure_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows_by_name["new-failures"]:
        failure_groups[str(row.get("capture_failure_reason") or "unknown_failure")].append(row)
    if full:
        for reason in sorted(failure_groups):
            selected.extend((reason, row, False) for row in failure_groups[reason])
    else:
        for reason in sorted(failure_groups):
            rows = failure_groups[reason]
            company_count = len({str(row.get("company_id") or "") for row in rows})
            selected.extend((reason, row, False) for row in _diverse_pick(rows, min(2, company_count), prefer_platform=False))

    missing_rows = rows_by_name["missing-jd"]
    selected.extend(
        ("missing_detail", row, False)
        for row in (missing_rows if full else _diverse_pick(missing_rows, 8, prefer_platform=True))
    )
    if not full and len(selected) > 40:
        raise ValueError("Live acceptance pilot exceeds 40-job bound")

    recipes = _load_company_recipes()
    samples = [
        _sample_record(
            index,
            stratum,
            row,
            existing=existing,
            profile=profile,
            company_recipe=recipes.get(str(row.get("company_id")))
            or recipes.get(str(row.get("company_name"))),
        )
        for index, (stratum, row, existing) in enumerate(selected)
    ]
    input_paths = [source / f"{name}.jsonl" for name in SOURCE_NAMES]
    manifest = {
        "schema": SCHEMA_VERSION,
        "read_only_network": True,
        "formal_database_writes": 0,
        "model_calls": 0,
        "samples": samples,
        "input_counts": actual_counts,
        "selection_counts": dict(Counter(item[0] for item in selected)),
        "selection_platforms": dict(Counter(item["platform"] for item in samples)),
        "selection_companies": len({str(item[1].get("company_id") or "") for item in selected}),
        "title_excluded_before_detail": 0,
        "inputs": [{"path": str(path.resolve()), "sha256": _sha256_file(path)} for path in input_paths],
    }
    _write_json(output / "selection.json", manifest)
    return manifest


def prepare_retry(
    previous: Path,
    output: Path,
    *,
    platforms: set[str] | None = None,
    statuses: set[str] | None = None,
    strata: set[str] | None = None,
    limit: int = 0,
) -> dict[str, Any]:
    """Build a new immutable manifest from failed results of an earlier run."""

    previous_manifest = _read_json(previous / "selection.json")
    previous_samples = {
        int(sample["index"]): sample for sample in previous_manifest.get("samples", [])
    }
    company_recipes = _load_company_recipes()
    selected: list[dict[str, Any]] = []
    result_paths = sorted(
        previous.glob("result-*.json"),
        key=lambda path: int(path.stem.rsplit("-", 1)[-1]),
    )
    for path in result_paths:
        result = _read_json(path)
        if bool(result.get("success")):
            continue
        platform = str(result.get("platform") or "")
        status = str(result.get("failure_reason") or result.get("status") or "")
        if platforms and platform not in platforms:
            continue
        if statuses and status not in statuses:
            continue
        original = previous_samples.get(int(result["index"]))
        if original is None:
            raise ValueError(f"Previous result has no bound sample: {path.name}")
        if strata and str(original.get("stratum") or "") not in strata:
            continue
        sample = deepcopy(original)
        recipe = (
            company_recipes.get(str(sample.get("company_id") or ""))
            or company_recipes.get(str(sample.get("company") or ""))
        )
        if recipe:
            for job_key in ("input_job", "request_job"):
                job = sample.get(job_key)
                if not isinstance(job, dict):
                    continue
                for key, value in recipe.items():
                    job[key] = deepcopy(value)
        sample["index"] = len(selected)
        sample["stratum"] = f"retry:{status}"
        sample["historical_failure_reason"] = status
        selected.append(sample)
        if limit and len(selected) >= limit:
            break
    manifest = {
        "schema": SCHEMA_VERSION,
        "read_only_network": True,
        "formal_database_writes": 0,
        "model_calls": 0,
        "samples": selected,
        "input_counts": previous_manifest.get("input_counts", {}),
        "selection_counts": dict(Counter(item["stratum"] for item in selected)),
        "selection_platforms": dict(Counter(item["platform"] for item in selected)),
        "selection_companies": len({str(item["company_id"]) for item in selected}),
        "title_excluded_before_detail": 0,
        "inputs": list(previous_manifest.get("inputs", [])),
        "retry_from": str(previous.resolve()),
    }
    _write_json(output / "selection.json", manifest)
    return manifest


def _assert_inputs_unchanged(manifest: Mapping[str, Any]) -> bool:
    return all(_sha256_file(Path(item["path"])) == item["sha256"] for item in manifest.get("inputs", []))


def _stable_failure_reason(response: Mapping[str, Any], validation: Mapping[str, Any], assessment: Any) -> str:
    status = str(response.get("status") or "").strip().casefold()
    known = {
        "cohort_ineligible",
        "identity_mismatch",
        "identity_ambiguous",
        "captcha_required",
        "render_failed",
        "content_incomplete",
        "timeout",
        "job_offline",
        "fetch_failed",
        "no_detail_url",
        "no_detail",
        "domain_blocked",
    }
    if status in known:
        return status
    if status in {"isolated_timeout", "isolated_worker_error", "exception"}:
        error_type = str(response.get("error_type") or "").casefold()
        if "timeout" in error_type or status == "isolated_timeout":
            return "isolated_timeout"
        if error_type:
            return f"isolated_{re.sub(r'[^a-z0-9]+', '_', error_type).strip('_')}"
        return status
    if status == "complete" and not assessment.complete:
        return f"capture_{assessment.reason_code}"
    reasons = [str(item) for item in validation.get("failure_reasons", []) if str(item)]
    if reasons:
        return "capture_" + re.sub(r"[^a-z0-9]+", "_", reasons[0].casefold()).strip("_")
    return status or "detail_capture_failed"


def run_sample(
    sample: Mapping[str, Any],
    output: Path,
    locks: Mapping[str, Lock],
    timeout: float,
    fetch=fetch_job_detail_result_isolated,
    domain_barriers: dict[str, str] | None = None,
) -> dict[str, Any]:
    path = output / f"result-{int(sample['index']):03}.json"
    if path.exists():
        return _read_json(path)
    job = deepcopy(dict(sample["request_job"]))
    domain = _host(job.get("detail_url")) or "no-domain"
    if domain_barriers is None:
        domain_barriers = {}
    with locks[domain]:
        started = time.monotonic()
        barrier = domain_barriers.get(domain)
        if barrier:
            response = {
                "status": "domain_blocked",
                "error_type": f"domain_barrier:{barrier}",
                "error": f"Skipped after same-domain terminal barrier: {barrier}",
                "detail": "",
                "detail_url": job.get("detail_url"),
            }
        else:
            try:
                response = dict(fetch(job, timeout_seconds=timeout))
            except IsolatedOperationTimeout as exc:
                response = {"status": "isolated_timeout", "error_type": type(exc).__name__, "error": str(exc), "detail": ""}
            except IsolatedWorkerError as exc:
                response = {
                    "status": "isolated_worker_error",
                    "error_type": exc.error_type or type(exc).__name__,
                    "error": str(exc),
                    "detail": "",
                }
            except Exception as exc:  # one live sample must not stop the bounded batch
                response = {"status": "exception", "error_type": type(exc).__name__, "error": str(exc)[:1200], "detail": ""}
            if str(response.get("status") or "") in DOMAIN_BARRIER_STATUSES:
                domain_barriers[domain] = str(response["status"])
    detail = str(response.get("detail") or "")
    evidence = response.get("capture_evidence") or {}
    assessment = assess_jd_capture({**job, "jd_raw": detail, "capture_evidence": evidence})
    validation = validate_candidate(job, response)
    success = response.get("status") == "complete" and assessment.complete and validation["passed"]
    result = {
        "schema": SCHEMA_VERSION,
        "index": sample["index"],
        "stratum": sample["stratum"],
        "platform": sample["platform"],
        "company_id": sample["company_id"],
        "company": sample["company"],
        "title": sample["title"],
        "job_id": sample["job_id"],
        "existing_job_ids": sample["existing_job_ids"],
        "input_job": sample["input_job"],
        "request_job": job,
        "request_detail_url": job.get("detail_url"),
        "official_detail_url": response.get("detail_url") or job.get("detail_url"),
        "response": response,
        "status": response.get("status") or "unknown",
        "success": success,
        "capture_assessment": {
            "complete": assessment.complete,
            "reason_code": assessment.reason_code,
            "reason": assessment.reason,
        },
        "validation": validation,
        "identity": {
            "status": response.get("identity_status") or "",
            "evidence": list(response.get("identity_evidence") or []),
            "diagnostic": response.get("identity_diagnostic") or {},
        },
        "jd_sha256": _sha256_text(detail),
        "capture_evidence": evidence,
        "failure_reason": None if success else _stable_failure_reason(response, validation, assessment),
        "historical_failure_reason": sample.get("historical_failure_reason"),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "tested_at": datetime.now(UTC).isoformat(),
        "formal_database_writes": 0,
        "model_calls": 0,
    }
    _write_json(path, result)
    print(json.dumps({"index": result["index"], "company": result["company"], "status": result["status"], "success": result["success"]}, ensure_ascii=False), flush=True)
    return result


def run_network_samples(manifest: Mapping[str, Any], output: Path, *, workers: int, timeout: float) -> list[dict[str, Any]]:
    locks = {_host(sample["request_job"].get("detail_url")) or "no-domain": Lock() for sample in manifest["samples"]}
    domain_barriers: dict[str, str] = {}
    for sample in manifest["samples"]:
        path = output / f"result-{int(sample['index']):03}.json"
        if path.exists():
            prior = _read_json(path)
            prior_status = str((prior.get("response") or {}).get("status") or "")
            if prior_status in DOMAIN_BARRIER_STATUSES:
                domain_barriers[_host(sample["request_job"].get("detail_url")) or "no-domain"] = prior_status
    # Interleave domains so same-domain locks do not occupy every worker while
    # unrelated career sites are waiting behind them.
    by_domain: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for sample in manifest["samples"]:
        by_domain[_host(sample["request_job"].get("detail_url")) or "no-domain"].append(sample)
    ordered_samples: list[Mapping[str, Any]] = []
    while by_domain:
        for domain in list(by_domain):
            bucket = by_domain[domain]
            if bucket:
                ordered_samples.append(bucket.pop(0))
            if not bucket:
                del by_domain[domain]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                run_sample,
                sample,
                output,
                locks,
                timeout,
                domain_barriers=domain_barriers,
            )
            for sample in ordered_samples
        ]
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda item: int(item["index"]))


def _sample_summary(results: list[Mapping[str, Any]], manifest: Mapping[str, Any]) -> dict[str, Any]:
    strata: dict[str, dict[str, int]] = {}
    for stratum in sorted({str(item["stratum"]) for item in results}):
        rows = [item for item in results if item["stratum"] == stratum]
        strata[stratum] = {
            "selected": len(rows),
            "verified_jd": sum(bool(item["success"]) for item in rows),
            "failed": sum(not bool(item["success"]) for item in rows),
        }
    return {
        "schema": SCHEMA_VERSION,
        "selected": len(results),
        "verified_jd": sum(bool(item["success"]) for item in results),
        "failed": sum(not bool(item["success"]) for item in results),
        "strata": strata,
        "failure_reasons": dict(Counter(str(item["failure_reason"]) for item in results if not item["success"])),
        "historical_failure_reasons": dict(Counter(str(item["historical_failure_reason"]) for item in results if item["historical_failure_reason"])),
        "platforms": dict(Counter(str(item["platform"]) for item in results)),
        "companies": len({str(item["company_id"]) for item in results}),
        "cohort_gate_blocks": sum(item["status"] == "cohort_ineligible" for item in results),
        "title_excluded_before_detail": manifest.get("title_excluded_before_detail", 0),
        "input_hashes_unchanged": _assert_inputs_unchanged(manifest),
        "formal_database_writes": 0,
        "model_calls": 0,
        "full_company_validation": False,
    }


def _dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _job_semantic(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "company_id": row.company_id,
        "title": row.title,
        "detail_url": row.detail_url,
        "jd_sha256": _sha256_text(row.jd_raw),
        "cohort": row.cohort,
        "cohort_status": row.cohort_status,
        "batch": row.batch,
        "match_score": row.match_score,
        "source_platform": row.source_platform,
        "source_tenant": row.source_tenant,
        "native_job_id": row.native_job_id,
        "capture_status": row.capture_status,
        "capture_failure_reason": row.capture_failure_reason,
        "availability_status": row.availability_status,
        "title_key": row.title_key,
        "capture_evidence": row.capture_evidence or {},
        "source": row.source,
        "source_ref": row.source_ref,
    }


def _job_model_semantic(model: Job) -> dict[str, Any]:
    return {
        "id": model.id,
        "company_id": model.company_id,
        "title": model.title,
        "detail_url": str(model.detail_url),
        "jd_sha256": _sha256_text(model.jd_raw),
        "cohort": model.cohort,
        "cohort_status": model.cohort_status,
        "batch": model.batch.value,
        "match_score": model.match_score,
        "source_platform": model.source_platform,
        "source_tenant": model.source_tenant,
        "native_job_id": model.native_job_id,
        "capture_status": model.capture_status,
        "capture_failure_reason": model.capture_failure_reason,
        "availability_status": model.availability_status,
        "title_key": model.title_key,
        "capture_evidence": model.capture_evidence or {},
        "source": model.source,
        "source_ref": model.source_ref,
    }


def _company_semantic(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "aliases": list(row.aliases or []),
        "campus_url": row.campus_url,
        "crawler_key": row.crawler_key,
        "integration_status": row.integration_status,
        "organization_id": row.organization_id,
        "recruitment_unit_name": row.recruitment_unit_name,
        "source_identity": row.source_identity,
        "source": row.source,
        "source_ref": row.source_ref,
    }


def _company_model_semantic(model: Company) -> dict[str, Any]:
    return {
        "id": model.id,
        "name": model.name,
        "aliases": list(model.aliases),
        "campus_url": model.campus_url,
        "crawler_key": model.crawler_key,
        "integration_status": model.integration_status,
        "organization_id": model.organization_id,
        "recruitment_unit_name": model.recruitment_unit_name,
        "source_identity": model.source_identity,
        "source": model.source,
        "source_ref": model.source_ref,
    }


def _application_semantic(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "company_name": row.company_name,
        "job_title": row.job_title,
        "job_id": row.job_id,
        "record_url": row.record_url,
        "stage": row.stage,
        "idempotency_key": row.idempotency_key,
        "note": row.note,
        "stage_history": row.stage_history or [],
        "source_stage": row.source_stage,
        "source_status": row.source_status,
        "source_status_synced_at": _dt(row.source_status_synced_at),
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
        "source": row.source,
        "source_ref": row.source_ref,
    }


def _source_semantic(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "source": row.source,
        "source_record_id": row.source_record_id,
        "company_name": row.company_name,
        "company_id": row.company_id,
        "source_url": row.source_url,
        "entry_url": row.entry_url,
        "original_entry_url": row.original_entry_url,
        "final_url": row.final_url,
        "status": row.status,
        "failure_stage": row.failure_stage,
        "reason_code": row.reason_code,
        "reason": row.reason,
        "job_count": row.job_count,
        "jd_pending_count": row.jd_pending_count,
        "last_success_job_count": row.last_success_job_count,
        "pagination_complete": row.pagination_complete,
    }


def _canonical_hash(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def database_snapshot(storage: Storage) -> dict[str, Any]:
    with storage.session() as session:
        companies = list(session.scalars(select(CompanySnapshot).order_by(CompanySnapshot.id)).all())
        jobs = list(session.scalars(select(JobSnapshot).order_by(JobSnapshot.id)).all())
        analyses = list(session.scalars(select(JobAnalysisSnapshot).order_by(JobAnalysisSnapshot.job_id)).all())
        applications = list(session.scalars(select(ApplicationSnapshot).order_by(ApplicationSnapshot.id)).all())
        sources = list(session.scalars(select(CompanySourceRecord).order_by(CompanySourceRecord.id)).all())
        attempts = list(session.scalars(select(CompanySourceAttempt).order_by(CompanySourceAttempt.id)).all())
    return {
        "counts": {
            "companies": len(companies),
            "jobs": len(jobs),
            "analyses": len(analyses),
            "applications": len(applications),
            "company_sources": len(sources),
            "company_source_attempts": len(attempts),
        },
        "hashes": {
            "companies": _canonical_hash([_company_semantic(row) for row in companies]),
            "jobs": _canonical_hash([_job_semantic(row) for row in jobs]),
            "analyses": _canonical_hash([
                {"job_id": row.job_id, "match_score": row.match_score, "analysis_status": row.analysis_status, "model": row.model}
                for row in analyses
            ]),
            "applications": _canonical_hash([_application_semantic(row) for row in applications]),
            "company_sources": _canonical_hash([_source_semantic(row) for row in sources]),
            "company_source_attempts": _canonical_hash([row.id for row in attempts]),
        },
        "old_job_ids": [row.id for row in jobs if row.id in _seed_old_ids(storage)],
        "application_rows": [_application_semantic(row) for row in applications],
    }


def _seed_old_ids(storage: Storage) -> set[str]:
    with storage.session() as session:
        rows = session.scalars(select(JobSnapshot).where(JobSnapshot.source == "title_first_acceptance_seed")).all()
        return {row.id for row in rows}


def _company_info(samples: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    info: dict[str, dict[str, Any]] = {}
    for sample in samples:
        company_id = str(sample["company_id"])
        info.setdefault(
            company_id,
            {
                "id": company_id,
                "name": sample["company"],
                "campus_url": (sample.get("source_urls") or [sample.get("input_job", {}).get("careers_url", "")])[0],
                "platform": sample["platform"],
            },
        )
    return info


def _seed_database(database: Path, samples: list[Mapping[str, Any]]) -> Storage:
    if database.exists():
        raise ValueError(f"Refusing to overwrite existing isolated database: {database}")
    database.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage.from_url(f"sqlite:///{database}")
    storage.initialize()
    fixed = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    info = _company_info(samples)
    old_samples = [sample for sample in samples if sample["existing_job_ids"]]
    with storage.write_transaction() as session:
        for item in info.values():
            session.add(
                    CompanySnapshot(
                    id=item["id"],
                    name=item["name"],
                    aliases=[],
                    campus_url=item["campus_url"] or None,
                    crawler_key=item["platform"],
                    integration_status="connected",
                    organization_id=item["id"],
                    recruitment_unit_name=item["name"],
                    source="title_first_acceptance_seed",
                    source_ref=f"seed:company:{item['id']}",
                    created_at=fixed,
                    updated_at=fixed,
                )
            )
        for sample in old_samples:
            job = sample["request_job"]
            model = Job(
                id=sample["job_id"],
                company_id=sample["company_id"],
                title=sample["title"],
                city=job.get("city") or None,
                detail_url=sample["detail_url"],
                jd_raw=None,
                cohort=2027,
                cohort_status="confirmed",
                batch=RecruitmentBatch.FORMAL,
                match_score=None,
                first_seen_at=fixed,
                last_seen_at=fixed,
                source_platform=sample["platform"],
                native_job_id=job.get("native_job_id"),
                capture_status="pending",
                capture_failure_reason="",
                availability_status="active",
                title_key=normalize_job_title_key(sample["title"]),
                capture_evidence={},
                source="title_first_acceptance_seed",
                source_ref=f"seed:job:{sample['job_id']}",
                created_at=fixed,
                updated_at=fixed,
            )
            upsert_job_snapshot(session, model)
        if old_samples:
            first = old_samples[0]
            session.add(
                JobAnalysisSnapshot(
                    job_id=first["job_id"],
                    match_score=77,
                    advantages="[\"seeded\"]",
                    gaps="[]",
                    summary="isolated preservation sentinel",
                    recommendation="preserve",
                    score_breakdown={"seed": 77},
                    evidence=[],
                    matched_directions=["cpp_software"],
                    analysis_status="complete",
                    model="fixture",
                    source="title_first_acceptance_seed",
                    source_ref=f"seed:analysis:{first['job_id']}",
                    created_at=fixed,
                    updated_at=fixed,
                )
            )
            session.add(
                ApplicationSnapshot(
                    id="isolated-application-sentinel",
                    company_name=first["company"],
                    job_title=first["title"],
                    job_id=first["job_id"],
                    record_url="https://example.invalid/application-sentinel",
                    stage=ApplicationStage.APPLIED.value,
                    idempotency_key="isolated:application-sentinel",
                    note="sentinel; not part of the acceptance operation",
                    stage_history=[{"stage": ApplicationStage.APPLIED.value, "at": fixed.isoformat()}],
                    source="title_first_acceptance_seed",
                    source_ref="seed:application:sentinel",
                    created_at=fixed,
                    updated_at=fixed,
                )
            )
    return storage


def _desired_job(sample: Mapping[str, Any], result: Mapping[str, Any], previous: Any, now: datetime) -> Job:
    success = bool(result["success"])
    detail = str(result["response"].get("detail") or "") if success else (str(previous.jd_raw or "") if previous else "")
    evidence = result.get("capture_evidence") or {} if success else (dict(previous.capture_evidence or {}) if previous else {})
    detail_url = str(result.get("official_detail_url") or sample["detail_url"]) if success else str(sample["detail_url"])
    created_at = previous.created_at if previous else now
    first_seen = previous.first_seen_at if previous and previous.first_seen_at else now
    return Job(
        id=sample["job_id"],
        company_id=sample["company_id"],
        title=sample["title"],
        city=sample["request_job"].get("city") or None,
        detail_url=detail_url,
        jd_raw=detail or None,
        cohort=2027,
        cohort_status="confirmed",
        batch=RecruitmentBatch.FORMAL,
        match_score=previous.match_score if previous else None,
        first_seen_at=first_seen,
        last_seen_at=now,
        recruitment_campaign_id=sample["request_job"].get("recruitment_campaign_id"),
        source_platform=sample["platform"],
        source_tenant=sample["request_job"].get("source_tenant"),
        native_job_id=sample["request_job"].get("native_job_id"),
        normalized_detail_url=detail_url,
        business_key=previous.business_key if previous else None,
        capture_status="complete" if success else "failed",
        capture_failure_reason="" if success else str(result["failure_reason"] or "detail_capture_failed"),
        availability_status="active",
        title_key=normalize_job_title_key(sample["title"]),
        capture_evidence=evidence,
        created_at=created_at,
        updated_at=now,
        source=previous.source if previous else ACCEPTANCE_SOURCE,
        source_ref=previous.source_ref if previous else f"acceptance:job:{sample['job_id']}",
    )


def _persist_pass(storage: Storage, samples: list[Mapping[str, Any]], results: list[Mapping[str, Any]]) -> dict[str, Any]:
    result_by_id = {str(result["job_id"]): result for result in results}
    now = datetime.now(UTC)
    counts = {
        "companies": Counter(),
        "jobs": Counter(),
        "company_sources": Counter(),
    }
    job_actions: dict[str, str] = {}
    company_models = []
    for item in _company_info(samples).values():
        company_models.append(
            Company(
                id=item["id"],
                name=item["name"],
                campus_url=item["campus_url"] or None,
                crawler_key=item["platform"],
                integration_status="partial",
                organization_id=item["id"],
                recruitment_unit_name=item["name"],
                created_at=now,
                updated_at=now,
                source=ACCEPTANCE_SOURCE,
                source_ref=f"acceptance:company:{item['id']}",
            )
        )
    with storage.write_transaction() as session:
        for model in company_models:
            row = session.get(CompanySnapshot, model.id)
            if row is None:
                upsert_company_snapshot(session, model)
                counts["companies"]["inserted"] += 1
            elif _company_semantic(row) == _company_model_semantic(model):
                counts["companies"]["reused"] += 1
            else:
                upsert_company_snapshot(session, model)
                counts["companies"]["updated"] += 1
        for sample in samples:
            previous = session.get(JobSnapshot, sample["job_id"])
            model = _desired_job(sample, result_by_id[sample["job_id"]], previous, now)
            if previous is None:
                action = "inserted"
                upsert_job_snapshot(session, model)
                counts["jobs"][action] += 1
            elif _job_semantic(previous) == _job_model_semantic(model):
                action = "reused"
                counts["jobs"][action] += 1
            else:
                action = "updated"
                upsert_job_snapshot(session, model)
                counts["jobs"][action] += 1
            job_actions[str(sample["job_id"])] = action

    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for sample, result in zip(samples, results, strict=True):
        source_url = str((sample.get("source_urls") or [sample["request_job"].get("careers_url", "")])[0] or "")
        grouped[(str(sample["company_id"]), source_url)].append(result)
    registry = CompanySourceRegistry(storage)
    source_actions: dict[str, str] = {}
    for (company_id, source_url), group in sorted(grouped.items()):
        source_record_id = f"{company_id}:{sha256(source_url.encode('utf-8')).hexdigest()}"
        failures = sorted({str(item["failure_reason"]) for item in group if not item["success"]})
        failure_count = sum(not bool(item["success"]) for item in group)
        reason_code = "detail_capture_failed" if failures else "sample_scope_incomplete"
        reason = (
            "Sampled detail failure(s): " + ", ".join(failures)
            if failures
            else "Detail sample is not a full-company pagination validation."
        )
        desired = {
            "source": ACCEPTANCE_SOURCE,
            "source_record_id": source_record_id,
            "company_name": group[0]["company"],
            "company_id": company_id,
            "source_url": source_url,
            "entry_url": source_url,
            "status": "partial",
            "failure_stage": "detail" if failures else "pagination",
            "reason_code": reason_code,
            "reason": reason,
            "job_count": len(group),
            "jd_pending_count": failure_count,
            "last_success_job_count": len(group),
            "pagination_complete": False,
        }
        with storage.session() as session:
            existing = session.scalar(
                select(CompanySourceRecord).where(
                    CompanySourceRecord.source == ACCEPTANCE_SOURCE,
                    CompanySourceRecord.source_record_id == source_record_id,
                )
            )
            # Source URLs are redacted by the registry before persistence.
            # Their original value is already bound into source_record_id, so
            # comparing the unredacted input with stored text would create a
            # duplicate attempt on every replay containing token-like keys.
            comparison_keys = {
                key
                for key in desired
                if key not in {"source", "source_record_id", "source_url", "entry_url"}
            }
            same = existing is not None and all(
                getattr(existing, key) == desired[key] for key in comparison_keys
            )
        if existing is None:
            record = registry.upsert_source(**{key: desired[key] for key in ("source", "source_record_id", "company_name", "source_url", "entry_url", "company_id")}, status="partial")
            registry.record_attempt(
                record["id"],
                status="partial",
                attempted_url=source_url,
                final_url=source_url,
                failure_stage=desired["failure_stage"],
                reason_code=reason_code,
                reason=reason,
                job_count=len(group),
                jd_pending_count=failure_count,
                pagination_complete=False,
            )
            source_actions[source_record_id] = "inserted"
            counts["company_sources"]["inserted"] += 1
        elif same:
            source_actions[source_record_id] = "reused"
            counts["company_sources"]["reused"] += 1
        else:
            registry.upsert_source(**{key: desired[key] for key in ("source", "source_record_id", "company_name", "source_url", "entry_url", "company_id")}, status="partial")
            registry.record_attempt(
                existing.id,
                status="partial",
                attempted_url=source_url,
                final_url=source_url,
                failure_stage=desired["failure_stage"],
                reason_code=reason_code,
                reason=reason,
                job_count=len(group),
                jd_pending_count=failure_count,
                pagination_complete=False,
            )
            source_actions[source_record_id] = "updated"
            counts["company_sources"]["updated"] += 1
    return {
        "counts": {key: dict(value) for key, value in counts.items()},
        "job_actions": job_actions,
        "source_actions": source_actions,
        "isolated_database_write_transactions": 1,
        "formal_database_writes": 0,
        "model_calls": 0,
        "applications_touched": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="title-first replay v2 directory")
    parser.add_argument("--output", type=Path, required=True, help="new isolated evaluation directory")
    parser.add_argument("--database", type=Path, help="new SQLite path under --output")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--full", action="store_true", help="Process all frozen failure/missing rows")
    parser.add_argument("--retry-from", type=Path, help="Retry failed records from an earlier acceptance directory")
    parser.add_argument("--retry-platform", action="append", default=[], help="Limit --retry-from to a platform family")
    parser.add_argument("--retry-status", action="append", default=[], help="Limit --retry-from to a stable failure reason")
    parser.add_argument("--retry-stratum", action="append", default=[], help="Limit --retry-from to an original sample stratum")
    parser.add_argument("--limit", type=int, default=0, help="Optional bounded retry count; zero means all selected failures")
    parser.add_argument(
        "--exclude-existing",
        action="store_true",
        help="Do not include the 14 rediscovered existing empty-JD repair rows",
    )
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    database = (args.database or output / "acceptance.sqlite").resolve()
    eval_root = (ROOT / ".data" / "evals").resolve()
    if not output.is_relative_to(eval_root) or output == source:
        raise ValueError("Use a new independent directory under .data/evals")
    if not database.is_relative_to(output):
        raise ValueError("The isolated database must be under the acceptance output directory")
    if args.limit < 0:
        raise ValueError("--limit must be zero or positive")
    retry_from = args.retry_from.resolve() if args.retry_from else None
    if retry_from is not None and (not retry_from.is_relative_to(eval_root) or retry_from == output):
        raise ValueError("--retry-from must be another directory under .data/evals")
    max_workers = 24 if args.full or retry_from is not None else 4
    if not 1 <= args.workers <= max_workers or not 1 <= args.timeout <= 60:
        raise ValueError(f"Bounds: workers 1..{max_workers}, timeout 1..60")
    output.mkdir(parents=True, exist_ok=True)
    profile = (yaml.safe_load((ROOT / "config" / "candidate_profile.yaml").read_text(encoding="utf-8")) or {}).get("profile", {})
    selection_path = output / "selection.json"
    manifest = (
        _read_json(selection_path)
        if selection_path.exists()
        else (
            prepare_retry(
                retry_from,
                output,
                platforms=set(args.retry_platform),
                statuses=set(args.retry_status),
                strata=set(args.retry_stratum),
                limit=args.limit,
            )
            if retry_from is not None
            else prepare(
                source,
                output,
                profile,
                full=args.full,
                include_existing=not args.exclude_existing,
            )
        )
    )
    for item in manifest["inputs"]:
        if _sha256_file(Path(item["path"])) != item["sha256"]:
            raise ValueError("Frozen input changed")
    if args.prepare_only:
        print(json.dumps({"selected": len(manifest["samples"]), "strata": manifest["selection_counts"], "platforms": manifest["selection_platforms"]}, ensure_ascii=False))
        return

    results = run_network_samples(manifest, output, workers=args.workers, timeout=args.timeout)
    summary = _sample_summary(results, manifest)
    _write_json(output / "summary.json", summary)
    if database.exists():
        raise ValueError(f"Refusing to overwrite existing isolated database: {database}")
    storage = _seed_database(database, manifest["samples"])
    before_seed = database_snapshot(storage)
    first = _persist_pass(storage, manifest["samples"], results)
    after_first = database_snapshot(storage)
    second = _persist_pass(storage, manifest["samples"], results)
    after_second = database_snapshot(storage)
    persistence = {
        "database": str(database),
        "before_acceptance_seed": before_seed,
        "run_1": first,
        "after_run_1": after_first,
        "run_2": second,
        "after_run_2": after_second,
        "idempotent": {
            "counts_equal": after_first["counts"] == after_second["counts"],
            "hashes_equal": after_first["hashes"] == after_second["hashes"],
            "old_ids_preserved": after_first["old_job_ids"] == after_second["old_job_ids"] == sorted(sample["job_id"] for sample in manifest["samples"] if sample["existing_job_ids"]),
            "applications_unchanged": after_first["hashes"]["applications"] == after_second["hashes"]["applications"] == before_seed["hashes"]["applications"],
            "analyses_unchanged": after_first["hashes"]["analyses"] == after_second["hashes"]["analyses"] == before_seed["hashes"]["analyses"],
            "source_attempts_second_run_reused": second["counts"]["company_sources"].get("inserted", 0) == 0 and second["counts"]["company_sources"].get("updated", 0) == 0,
        },
    }
    _write_json(output / "persistence.json", persistence)
    for result in results:
        result["persistence"] = {
            "run_1": first["job_actions"][str(result["job_id"])],
            "run_2": second["job_actions"][str(result["job_id"])],
        }
        _write_json(output / f"result-{int(result['index']):03}.json", result)
    summary.update(
        {
            "isolated_database": str(database),
            "persistence_idempotent": persistence["idempotent"],
            "database_counts_after_run_1": after_first["counts"],
            "database_counts_after_run_2": after_second["counts"],
        }
    )
    _write_json(output / "summary.json", summary)
    print(json.dumps({"summary": summary, "persistence": persistence["idempotent"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
