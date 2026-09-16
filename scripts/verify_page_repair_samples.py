"""Bounded, read-only D-acceptance probe for the 15 diagnosed pages."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import re
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCHEMA, MAX_COMPANIES, MAX_WORKERS, MAX_SAMPLES = 1, 15, 2, 2
OUT = ROOT / ".data/evals/page-repair-20260906"
DIAG = ROOT / "docs/CRAWL_PAGE_DIAGNOSIS_20260906.md"
BASE = ROOT / ".data/evals/oc_feishu_timeout_final.json"
ROUND4 = ROOT / ".data/evals/regression-20260905-round4/comparison.json"
INDEX = (
    ("大漠大智控", "大漠大智控", "大漠大智控", ("Damo",)),
    ("CVTE视源股份-海外留学生招聘专项", "CVTE视源股份-海外留学生招聘专项", "CVTE 海外专项", ("CVTE",)),
    ("Token Foundry", "Token Foundry", "Token Foundry", ()), ("得物", "得物", "得物", ()),
    ("拼多多", "拼多多", "拼多多", ()), ("惠科股份", "惠科股份", "惠科", ()),
    ("拓邦股份", "拓邦股份", "拓邦", ()), ("福耀集团", "福耀集团", "福耀", ()),
    ("沛睿微电子", "沛睿微电子", "沛睿微电子", ("Raymx",)), ("浪潮集团", "浪潮集团", "浪潮", ()),
    ("BIGO", "BIGO", "BIGO", ()), ("万凯新材", "万凯新材", "万凯新材", ()),
    ("中科蓝讯", "中科蓝讯", "中科蓝讯", ()), ("中望软件", "中望软件", "中望软件", ()),
    ("同花顺", "同花顺", "同花顺", ()),
)
TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")
DIAG_KEYS = ("page", "count", "total", "reason", "changed", "scope_hash")
PROJECT_DIAG_KEYS = ("project_id", "old_project_id", "observed_project_id", "observed_project_name", "page", "pages_seen", "total_pages", "advertised_total", "observed_total", "observed_unique", "scope_changed", "pagination_complete", "has_more", "fetch_failed", "termination_reason")
SECRET_QUERY_KEY = re.compile(r"(?:access[_-]?token|api[_-]?key|apikey|auth(?:orization)?|cookie|credential|jwt|password|passwd|secret|session|signature|sig|token)", re.I)
SENSITIVE_TEXT = re.compile(r"(?:authorization|bearer|cookie|set-cookie|token\s*[=:]|access[_-]?token|api[_-]?key|apikey|password|passwd|secret|session|signature|sig=)", re.I)
IDENTITY_EVIDENCE_KEY = re.compile(r"^(?:city|department|detail_url|id|jd_url|job_id|name|native_id|native_job_id|request_id|source_id|source_job_id|title|url):", re.I)
_FORMAL_JD_RULE = None
_FORMAL_SAMPLE_RULES = None
_RUNTIME = None
_OC_TRUSTED_SOURCE = None


class IsolatedOperationTimeout(TimeoutError):
    pass


class IsolatedWorkerError(RuntimeError):
    def __init__(self, message: str = "", *, error_type: str | None = None):
        super().__init__(message)
        self.error_type = error_type


def _runtime():
    global _RUNTIME, IsolatedOperationTimeout, IsolatedWorkerError
    if _RUNTIME is None:
        from packages.pipeline import isolation
        _RUNTIME = isolation
        IsolatedOperationTimeout, IsolatedWorkerError = isolation.IsolatedOperationTimeout, isolation.IsolatedWorkerError
    return _RUNTIME


def crawl_company_result_isolated(company: Mapping[str, object], *, timeout_seconds: float):
    return _runtime().crawl_company_result_isolated(company, timeout_seconds=timeout_seconds)


def fetch_job_detail_result_isolated(job: Mapping[str, object], *, timeout_seconds: float):
    return _runtime().fetch_job_detail_result_isolated(job, timeout_seconds=timeout_seconds)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _oc_trusted_source() -> str:
    global _OC_TRUSTED_SOURCE
    if _OC_TRUSTED_SOURCE is None:
        from packages.recruitment_core import job_cohorts
        _OC_TRUSTED_SOURCE = job_cohorts.OC_TRUSTED_SOURCE
    return _OC_TRUSTED_SOURCE


def _source_cohort_context(source: Mapping[str, object]) -> dict[str, object]:
    provenance = source.get("provenance") if isinstance(source.get("provenance"), Mapping) else {}
    lead_key = str(source.get("lead_key") or provenance.get("baseline_lead_key") or "").strip()
    projects = source.get("source_projects") or provenance.get("baseline_source_projects") or []
    if isinstance(projects, str):
        projects = [projects]
    projects = [str(item).strip() for item in projects if str(item).strip()]
    baseline_sha256 = str(source.get("baseline_sha256") or provenance.get("baseline_sha256") or "").strip()
    evidence = f"OC冻结基线 baseline_sha256={baseline_sha256}; lead_key={lead_key}; source_projects={'|'.join(projects)}"
    return {
        "source_cohort": 2027,
        "source_cohort_source": _oc_trusted_source(),
        "source_cohort_url": source["source_url"],
        "source_cohort_evidence": evidence[:500],
        "lead_key": lead_key,
        "source_projects": projects,
    }


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _rows(path: Path, keys: tuple[str, ...]) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in keys:
        rows = payload.get(key) if isinstance(payload, Mapping) else None
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, Mapping)]
    raise ValueError(f"row list missing: {path}")


def _diagnosis_url(text: str, heading: str) -> tuple[str, int]:
    active = False
    for line_no, line in enumerate(text.splitlines(), 1):
        if line.startswith("### "):
            active = heading in line
        if active and "原入口：" in line:
            match = re.search(r"\((https?://[^)]+)\)", line)
            if match:
                return match.group(1), line_no
    raise ValueError(f"diagnosis source missing: {heading}")


def resolve_sources(diagnosis: Path = DIAG, baseline: Path = BASE, round4: Path = ROUND4) -> list[dict[str, object]]:
    text, baseline_text, base_rows = diagnosis.read_text(encoding="utf-8"), baseline.read_text(encoding="utf-8"), _rows(baseline, ("results", "rows"))
    baseline_sha256 = _hash(baseline_text)
    r4_rows = _rows(round4, ("rows", "results")) if round4.exists() else []
    result = []
    for company, base_name, heading, aliases in INDEX:
        url, line_no = _diagnosis_url(text, heading)
        matches = [row for row in base_rows if row.get("company") == base_name]
        if len(matches) != 1 or matches[0].get("source_url") != url:
            raise ValueError(f"diagnosis/baseline source mismatch: {company}")
        r4 = [row for row in r4_rows if row.get("company") == base_name]
        if any(row.get("source_url") != url for row in r4):
            raise ValueError(f"diagnosis/round4 source mismatch: {company}")
        crawler = str(matches[0].get("crawler_key") or "")
        if not crawler:
            raise ValueError(f"crawler missing in baseline: {company}")
        lead_key = str(matches[0].get("lead_key") or "").strip()
        source_projects = matches[0].get("source_projects") or []
        if isinstance(source_projects, str):
            source_projects = [source_projects]
        source_projects = [str(item).strip() for item in source_projects if str(item).strip()]
        if not lead_key or not source_projects:
            raise ValueError(f"OC frozen baseline evidence missing: {company}")
        result.append({
            "company": company, "aliases": list(aliases), "source_url": url, "crawler": crawler,
            "lead_key": lead_key, "source_projects": source_projects, "baseline_sha256": baseline_sha256,
            "provenance": {"diagnosis": f"docs/CRAWL_PAGE_DIAGNOSIS_20260906.md:{line_no}", "baseline_company": base_name,
                            "baseline_source_url": matches[0].get("source_url"), "baseline_artifact": str(matches[0].get("artifact") or ""),
                            "baseline_lead_key": lead_key, "baseline_source_projects": source_projects, "baseline_sha256": baseline_sha256,
                            "round4_match": bool(r4), "round4_source_url": r4[0].get("source_url") if r4 else None,
                            "round4_artifact": str(r4[0].get("artifact") or "") if r4 else ""},
        })
    if len(result) != MAX_COMPANIES or len({row["source_url"] for row in result}) != MAX_COMPANIES:
        raise ValueError("expected 15 unique diagnosed source URLs")
    return result


def select_sources(sources: list[dict[str, object]], names: list[str] | None) -> list[dict[str, object]]:
    if not names:
        return sources
    result = []
    for name in dict.fromkeys(names):
        found = [row for row in sources if name in {row["company"], *row["aliases"], row["provenance"]["baseline_company"]}]
        if len(found) != 1:
            raise ValueError(f"--company must resolve uniquely: {name}")
        result.append(found[0])
    return result


def _code(value: object, default: str = "redacted_error") -> str:
    if isinstance(value, Mapping):
        value = value.get("error_code") or value.get("status")
    text = str(value or "").strip().casefold()
    if not text:
        return default
    for needle, code in (("proxy", "proxy_error"), ("timeout", "timeout"), ("login", "login_required"), ("captcha", "captcha_required"), ("verify", "captcha_required"), ("access", "access_denied")):
        if needle in text:
            return code
    return text if TOKEN.fullmatch(text) else default


def _error(code: object, error_type: object = "", stage: str = "list") -> dict[str, str]:
    item = {"stage": stage, "code": _code(code)}
    if error_type:
        item["type"] = _code(error_type, "")
    return item


def _unique(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return list({json.dumps(item, sort_keys=True): item for item in items}.values())


def _pagination_state(result: Mapping[str, object]) -> str:
    value = result.get("pagination_state")
    if value in {"complete", "incomplete", "unknown"}:
        return str(value)
    if result.get("pagination_complete") is True:
        return "complete"
    if result.get("pagination_complete") is False and result.get("completeness_known") is True:
        return "incomplete"
    return "unknown"


def _safe_diagnostics(value: object) -> list[dict[str, object]]:
    output = []
    for item in list(value or [])[:30]:
        if not isinstance(item, Mapping):
            continue
        safe = {}
        for key in DIAG_KEYS:
            val = item.get(key)
            if key in {"page", "count", "total"} and type(val) is int and val >= 0:
                safe[key] = val
            elif key == "changed" and type(val) is bool:
                safe[key] = val
            elif key == "reason" and val:
                safe[key] = _code(val, "redacted_reason")
            elif key == "scope_hash" and re.fullmatch(r"[0-9a-f]{8,64}", str(val or "")):
                safe[key] = str(val)
        if safe:
            output.append(safe)
    return output


def _pagination(result: Mapping[str, object]) -> dict[str, object]:
    boolean = lambda key: result.get(key) if type(result.get(key)) is bool else None
    integer = lambda key: result.get(key) if type(result.get(key)) is int and result.get(key) >= 0 else None
    return {"pagination_state": _pagination_state(result), "pagination_complete": boolean("pagination_complete"), "completeness_known": boolean("completeness_known"),
            "pagination_evidence_missing": result.get("error_code") == "pagination_evidence_missing", "pages_seen": integer("pages_seen"), "total_pages": integer("total_pages"),
            "advertised_total": integer("advertised_total"), "has_more": boolean("has_more"), "pagination_diagnostics": _safe_diagnostics(result.get("pagination_diagnostics"))}


def _safe_url(value: object) -> str | None:
    parsed = urlsplit(str(value or ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _public_url(value: object) -> str | None:
    """Keep an exact public URL, including fragments, while rejecting secret query keys."""
    text = str(value or "").strip()
    if not text or any(ord(char) < 32 for char in text):
        return None
    try:
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            return None
        if any(SECRET_QUERY_KEY.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            return None
    except ValueError:
        return None
    return text


def _safe_text(value: object, *, limit: int = 240) -> str | None:
    if not isinstance(value, (str, int, float, bool)):
        return None
    text = str(value).strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text) or SENSITIVE_TEXT.search(text):
        return None
    return text


def _safe_project_diagnostics(value: object) -> list[dict[str, object]]:
    output = []
    for item in list(value or [])[:20]:
        if not isinstance(item, Mapping):
            continue
        safe = {}
        for key in PROJECT_DIAG_KEYS:
            val = item.get(key)
            if key in {"page", "pages_seen", "total_pages", "advertised_total", "observed_total", "observed_unique"} and type(val) is int and val >= 0:
                safe[key] = val
            elif key in {"scope_changed", "pagination_complete", "has_more", "fetch_failed"} and type(val) is bool:
                safe[key] = val
            elif key == "termination_reason" and val:
                text = _safe_text(val, limit=160)
                if text:
                    safe[key] = text
            elif key in {"project_id", "old_project_id", "observed_project_id", "observed_project_name"}:
                text = _safe_text(val, limit=160)
                if text:
                    safe[key] = text
        if safe:
            output.append(safe)
    return output


def _safe_identity_evidence(value: object) -> list[str]:
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    output = []
    for raw in values[:20]:
        text = _safe_text(raw)
        if text and IDENTITY_EVIDENCE_KEY.match(text):
            output.append(text)
    return output


def _hydrator_diagnostics(detail: Mapping[str, object] | None) -> dict[str, object]:
    detail = detail or {}
    return {
        "identity_status": _code(detail.get("identity_status"), "") if detail.get("identity_status") else None,
        "identity_evidence": _safe_identity_evidence(detail.get("identity_evidence")),
        "source": _safe_text(detail.get("source"), limit=120),
        "attempts": [text for raw in list(detail.get("attempts") or [])[:20] if (text := _safe_text(raw, limit=160))],
    }


def _job_urls(job: Mapping[str, object]) -> tuple[str | None, str]:
    values = [job.get("jd_url"), job.get("detail_url")]
    safe_values = []
    for value in values:
        if value in (None, ""):
            continue
        safe = _public_url(value)
        if safe is None:
            return None, "unsafe_public_url"
        safe_values.append(safe)
    return (safe_values[0], "ok") if safe_values else (None, "missing")


def _observed_sources(result: Mapping[str, object], requested: str) -> list[dict[str, object]]:
    values = [{"source_url": requested, "kind": "requested"}]
    if result.get("project_diagnostics"):
        values[0]["project_diagnostics"] = _safe_project_diagnostics(result.get("project_diagnostics"))
    for run in result.get("source_runs") or []:
        if not isinstance(run, Mapping) or not _safe_url(run.get("source_url")):
            continue
        item: dict[str, object] = {"source_url": _safe_url(run.get("source_url")), "kind": "attempt", "pagination_state": _pagination_state(run), "pagination_diagnostics": _safe_diagnostics(run.get("pagination_diagnostics")), "project_diagnostics": _safe_project_diagnostics(run.get("project_diagnostics"))}
        if run.get("error_code"):
            item["error_code"] = _code(run.get("error_code"))
        values.append(item)
    for raw in result.get("effective_source_urls") or []:
        if (url := _safe_url(raw)):
            values.append({"source_url": url, "kind": "effective"})
    return list({json.dumps(item, sort_keys=True): item for item in values}.values())


def _result_errors(result: Mapping[str, object]) -> list[dict[str, str]]:
    errors = [_error(result["error_code"])] if result.get("error_code") else []
    for failure in result.get("failures") or []:
        code = (failure.get("error_code") or failure.get("code") or failure.get("status")) if isinstance(failure, Mapping) else failure
        if code:
            errors.append(_error(code))
    for failure in result.get("replay_errors") or []:
        code = failure.get("code") if isinstance(failure, Mapping) else failure
        if code:
            errors.append(_error(code, stage="replay_source"))
    for run in result.get("source_runs") or []:
        if isinstance(run, Mapping) and run.get("error_code"):
            errors.append(_error(run["error_code"], stage="source_run"))
    return _unique(errors)


def _classification(result: Mapping[str, object], jobs: list[Mapping[str, object]], errors: list[dict[str, str]]) -> str:
    values = " ".join([str(result.get("error_code") or "").casefold(), *(item["code"] for item in errors)])
    if any(marker in values for marker in ("login_required", "captcha_required", "access_denied", "blocked")):
        return "blocked"
    state = _pagination_state(result)
    if not jobs and state == "complete" and str(result.get("error_code") or "") in {"", "activity_empty", "no_results"}:
        return "empty"
    return "nonempty" if jobs else "unknown"


def _jd_text(job: Mapping[str, object]) -> str:
    if "jd_raw" in job:
        return str(job.get("jd_raw") or "")
    return "\n".join(str(job.get(key) or "") for key in ("description", "requirement", "content") if job.get(key))


def _jd_incomplete(job: Mapping[str, object]) -> bool:
    global _FORMAL_JD_RULE
    if _FORMAL_JD_RULE is None:
        from packages.matching import is_jd_incomplete
        _FORMAL_JD_RULE = is_jd_incomplete
    return bool(_FORMAL_JD_RULE({**job, "jd_raw": _jd_text(job)}))


def _sample_exclusion(job: Mapping[str, object]) -> str | None:
    global _FORMAL_SAMPLE_RULES
    if _FORMAL_SAMPLE_RULES is None:
        from packages.recruitment_core import job_filters
        _FORMAL_SAMPLE_RULES = job_filters
    normalized = {**job, "jd_raw": _jd_text(job)}
    if _FORMAL_SAMPLE_RULES.is_intern_job(normalized):
        return "internship"
    if _FORMAL_SAMPLE_RULES.is_doctorate_only_job(normalized):
        return "doctorate_only"
    batch = str(job.get("batch") or job.get("recruitment_track") or _FORMAL_SAMPLE_RULES.recruitment_track(normalized) or "").strip().casefold()
    if batch and batch not in {"formal", "formal_batch", "formalbatch", "early", "early_batch", "提前批"}:
        return "non_qualifying_batch"
    if _FORMAL_SAMPLE_RULES.is_job_record_noise(normalized):
        return "non_job_record"
    if _FORMAL_SAMPLE_RULES.is_social_job(normalized):
        return "non_displayable"
    return None


def _job_id(job: Mapping[str, object]) -> tuple[str | None, str | None]:
    for key in ("source_job_id", "native_job_id", "id", "job_id", "jobId", "postId", "positionId", "jobAdId"):
        if job.get(key) is not None and str(job[key]).strip():
            return str(job[key]), key
    return None, None


def _job_evidence(job: Mapping[str, object], result: Mapping[str, object], requested: str) -> dict[str, object]:
    jd_url, _ = _job_urls(job)
    source_list_url = next((url for value in (job.get("source_list_url"), job.get("list_url"), result.get("source_url"), requested) if (url := _public_url(value))), None)
    source_url = next((url for value in (job.get("source_url"), result.get("source_url"), requested) if (url := _public_url(value))), None)
    observed_source = _public_url(job.get("detail_link_source_url"))
    return {
        "title": str(job.get("title") or "")[:500],
        "id": _job_id(job)[0],
        "city": str(job.get("city") or "")[:300] or None,
        "jd_url": jd_url,
        "jd_raw": _jd_text(job)[:12000],
        "link_kind": str(job.get("link_kind") or "")[:80] or None,
        "source_list_url": source_list_url,
        "source_url": source_url,
        "observed_proof": {
            "detail_link_observed": job.get("detail_link_observed") is True,
            "detail_link_source_url": observed_source,
        },
    }


def _sample(jobs: list[Mapping[str, object]], result: Mapping[str, object], requested: str, timeout: float, detail_runner: Callable[..., Mapping[str, object]]):
    missing = [job for job in jobs if _jd_incomplete(job)]
    excluded = [job for job in missing if _sample_exclusion(job)]
    candidates = [job for job in missing if not _sample_exclusion(job)]
    samples, errors = [], []
    for index, job in enumerate(candidates[:MAX_SAMPLES]):
        raw, job_id, field = _jd_text(job), *_job_id(job)
        evidence = _job_evidence(job, result, requested)
        item: dict[str, object] = {"sample_scope": "sample", "nonall": True, "sample_index": index, "job_id": job_id, "job_id_field": field, "source_job_id": job.get("source_job_id"), "native_job_id": job.get("native_job_id"), "title": evidence["title"], "jd_url": evidence["jd_url"], "source_list_url": evidence["source_list_url"], "source_url": evidence["source_url"], "observed_proof": evidence["observed_proof"], "original_jd_length": len(raw), "original_jd_sha256": _hash(raw), "detail_attempted": False, "detail_status": "not_attempted", "hydrator": _hydrator_diagnostics(None), "identity_status": None, "identity_evidence": [], "source": None, "attempts": []}
        item["sample_verified"] = False
        detail_url, url_status = _job_urls(job)
        if url_status == "missing":
            item.update(detail_status="not_addressable", error=_error("detail_url_missing", stage="jd_sample"))
        elif url_status != "ok" or not detail_url:
            item.update(detail_status="not_addressable", error=_error("unsafe_public_url", stage="jd_sample"))
        else:
            item["detail_attempted"] = True
            try:
                detail_job = dict(job); detail_job["jd_url"] = detail_url; detail_job["detail_url"] = detail_url
                detail = detail_runner(detail_job, timeout_seconds=min(timeout, 45.0)); text = str(detail.get("detail") or "") if isinstance(detail, Mapping) else ""
                status = _code(detail.get("status"), "content_incomplete") if isinstance(detail, Mapping) else "content_incomplete"
                hydrator = _hydrator_diagnostics(detail if isinstance(detail, Mapping) else None)
                item.update(detail_status=status, fetched_jd_length=len(text), fetched_jd_sha256=_hash(text), hydrator=hydrator, identity_status=hydrator["identity_status"], identity_evidence=hydrator["identity_evidence"], source=hydrator["source"], attempts=hydrator["attempts"])
                if status != "complete":
                    item["error"] = _error(status, detail.get("error_type") if isinstance(detail, Mapping) else "", "jd_sample")
                elif hydrator["identity_status"] not in {"matched", "request_bound"} or not hydrator["identity_evidence"]:
                    item["error"] = _error("identity_evidence_missing", stage="jd_sample")
                else:
                    item["sample_verified"] = True
            except IsolatedOperationTimeout as exc:
                item.update(detail_status="timeout", error=_error("timeout", type(exc).__name__, "jd_sample"))
            except IsolatedWorkerError as exc:
                item.update(detail_status="worker_failed", error=_error("worker_failed", exc.error_type, "jd_sample"))
            except Exception as exc:
                item.update(detail_status="fetch_failed", error=_error("fetch_failed", type(exc).__name__, "jd_sample"))
        if item.get("error"):
            errors.append(item["error"])
        samples.append(item)
    return samples, _unique(errors), candidates, excluded


def _annotate_replay_jobs(source: Mapping[str, object], jobs: list[dict[str, object]]) -> list[dict[str, object]]:
    from packages.recruitment_core import job_cohorts
    context = _source_cohort_context(source)
    campaign = job_cohorts.trusted_source_campaign(context, jobs_observed=bool(jobs))
    if campaign is None:
        raise ValueError(f"frozen OC context cannot annotate replay jobs: {source['company']}")
    return job_cohorts.annotate_company_jobs(jobs, str(source["source_url"]), inspect_page=False, campaign=campaign)


def _replay_result(row: Mapping[str, object], jobs: list[dict[str, object]]) -> dict[str, object]:
    pagination = row.get("pagination") if isinstance(row.get("pagination"), Mapping) else {}
    counts = row.get("counts") if isinstance(row.get("counts"), Mapping) else {}
    source_runs = []
    effective_urls = []
    for item in row.get("sources") or []:
        if not isinstance(item, Mapping) or item.get("kind") != "attempt":
            continue
        source_url = str(item.get("source_url") or "")
        if not source_url:
            continue
        source_runs.append({
            "source_url": source_url,
            "pagination_state": item.get("pagination_state"),
            "pagination_diagnostics": item.get("pagination_diagnostics") or [],
            "project_diagnostics": item.get("project_diagnostics") or [],
            "error_code": item.get("error_code"),
        })
        effective_urls.append(source_url)
    replay_errors, previous_jd_errors = [], []
    for item in row.get("errors") or []:
        if not isinstance(item, Mapping):
            continue
        stage = str(item.get("stage") or "").casefold()
        (previous_jd_errors if stage == "jd_sample" or stage.startswith("jd_") or stage in {"hydration", "detail"} else replay_errors).append(dict(item))
    return {
        "jobs": jobs,
        "raw_job_count": counts.get("observed_jobs", len(jobs)),
        "accepted_count": counts.get("reported_accepted_count"),
        "complete_jd_count": counts.get("reported_complete_jd_count"),
        "incomplete_jd_count": counts.get("reported_incomplete_jd_count"),
        "pagination_state": pagination.get("pagination_state", "unknown"),
        "pagination_complete": pagination.get("pagination_complete"),
        "completeness_known": pagination.get("completeness_known"),
        "pages_seen": pagination.get("pages_seen"),
        "total_pages": pagination.get("total_pages"),
        "advertised_total": pagination.get("advertised_total"),
        "has_more": pagination.get("has_more"),
        "pagination_diagnostics": pagination.get("pagination_diagnostics") or [],
        "error_code": "pagination_evidence_missing" if pagination.get("pagination_evidence_missing") else None,
        "termination_reasons": row.get("termination") or ["replay_artifact_reused"],
        "source_runs": source_runs,
        "effective_source_urls": effective_urls,
        "replay_errors": replay_errors,
        "previous_jd_errors": previous_jd_errors,
    }


def load_replay(replay_dir: Path, sources: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    root = replay_dir.resolve()
    report_path = root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows = {str(row.get("company")): row for row in report.get("results", []) if isinstance(row, Mapping)}
    replay = {}
    for source in sources:
        row = rows.get(str(source["company"]))
        if row is None or row.get("source_url") != source["source_url"]:
            raise ValueError(f"replay report company/source mismatch: {source['company']}")
        reference = str(row.get("job_artifact") or "")
        if not reference:
            raise ValueError(f"replay artifact missing: {source['company']}")
        artifact_path = (root / reference).resolve()
        if not artifact_path.is_relative_to(root):
            raise ValueError(f"replay artifact escapes report directory: {source['company']}")
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        if artifact.get("company") != source["company"] or artifact.get("source_url") != source["source_url"]:
            raise ValueError(f"replay artifact company/source mismatch: {source['company']}")
        inventory = {str(item.get("jd_url")): item for item in row.get("jd_inventory", []) if isinstance(item, Mapping) and item.get("jd_url")}
        jobs = []
        for raw in artifact.get("jobs", []):
            if not isinstance(raw, Mapping):
                continue
            proof = raw.get("observed_proof") if isinstance(raw.get("observed_proof"), Mapping) else {}
            old = inventory.get(str(raw.get("jd_url") or ""), {})
            job = {
                "id": raw.get("id") or old.get("job_id"),
                "source_job_id": old.get("source_job_id"),
                "native_job_id": old.get("native_job_id"),
                "title": raw.get("title") or "",
                "city": raw.get("city"),
                "jd_url": raw.get("jd_url"),
                "detail_url": raw.get("jd_url"),
                "jd_raw": raw.get("jd_raw") or "",
                "link_kind": raw.get("link_kind"),
                "source_list_url": raw.get("source_list_url"),
                "source_url": raw.get("source_url") or source["source_url"],
                "detail_link_observed": proof.get("detail_link_observed") is True,
                "detail_link_source_url": proof.get("detail_link_source_url"),
            }
            jobs.append(job)
        replay[source["company"]] = _replay_result(row, _annotate_replay_jobs(source, jobs))
    return replay


def _failed(source: Mapping[str, object], code: str, error_type: object, started_at: str, started: float) -> dict[str, object]:
    error = _error(code, error_type)
    return {"company": source["company"], "source_url": source["source_url"], "crawler": source["crawler"], "started_at": started_at, "completed_at": _now(), "elapsed_seconds": round(time.monotonic() - started, 3), "classification": "blocked" if code in {"login_required", "captcha_required", "access_denied"} else "unknown", "pagination": {"pagination_state": "unknown", "pagination_complete": None, "completeness_known": None, "pagination_evidence_missing": False, "pages_seen": None, "total_pages": None, "advertised_total": None, "has_more": None, "pagination_diagnostics": []}, "counts": {"observed_jobs": 0, "invalid_job_records": 0, "reported_raw_job_count": None, "reported_accepted_count": None, "reported_complete_jd_count": None, "reported_incomplete_jd_count": None, "advertised_total": None, "missing_jd_total": 0, "missing_jd_sampled": 0, "missing_jd_not_sampled": 0, "missing_jd_excluded": 0, "excluded_count": 0}, "termination": [error["code"]], "errors": [error], "previous_jd_errors": [], "sources": [{"source_url": source["source_url"], "kind": "requested"}], "jd_samples": [], "jd_inventory": [], "replay": False, "list_fetch": "isolated_worker", "_job_artifact": {"schema": SCHEMA, "company": source["company"], "source_url": source["source_url"], "jobs": [], "read_only": True}, "read_only": True, "model_calls": 0, "database_writes": 0, "config_writes": 0}


def verify_company(source: Mapping[str, object], *, timeout_seconds: float, list_runner=None, detail_runner=None, frozen_result: Mapping[str, object] | None = None) -> dict[str, object]:
    started_at, started = _now(), time.monotonic(); list_runner = list_runner or crawl_company_result_isolated; detail_runner = detail_runner or fetch_job_detail_result_isolated
    cohort_context = _source_cohort_context(source)
    replay = frozen_result is not None
    if replay:
        result = dict(frozen_result)
    else:
        payload = {"id": f"page-repair-{source['company']}", "name": source["company"], "careers_url": source["source_url"], "crawler": source["crawler"], "integration_status": "connected", **cohort_context}
        try:
            result = dict(list_runner(payload, timeout_seconds=timeout_seconds))
        except IsolatedOperationTimeout as exc:
            return _failed(source, "timeout", type(exc).__name__, started_at, started)
        except IsolatedWorkerError as exc:
            return _failed(source, "worker_failed", exc.error_type, started_at, started)
        except Exception as exc:
            return _failed(source, "crawler_failed", type(exc).__name__, started_at, started)
    raw_jobs = result.get("jobs") if isinstance(result.get("jobs"), list) else []; jobs = [job for job in raw_jobs if isinstance(job, Mapping)]
    errors = _result_errors(result); samples, sample_errors, candidates, excluded = _sample(jobs, result, source["source_url"], timeout_seconds, detail_runner); errors = _unique(errors + sample_errors)
    termination = list(dict.fromkeys([_code(x) for x in result.get("termination_reasons") or [] if x] + ([_code(result["error_code"])] if result.get("error_code") else [])))
    safe_jobs = [_job_evidence(job, result, source["source_url"]) for job in jobs]
    inventory = [{"job_id": _job_id(job)[0], "job_id_field": _job_id(job)[1], "source_job_id": job.get("source_job_id"), "native_job_id": job.get("native_job_id"), "title": safe["title"], "jd_url": safe["jd_url"], "source_list_url": safe["source_list_url"], "source_url": safe["source_url"], "observed_proof": safe["observed_proof"], "original_jd_length": len(_jd_text(job)), "original_jd_sha256": _hash(_jd_text(job))} for job, safe in zip(jobs, safe_jobs)]
    counts = {"observed_jobs": len(jobs), "invalid_job_records": len(raw_jobs) - len(jobs), "reported_raw_job_count": result.get("raw_job_count"), "reported_accepted_count": result.get("accepted_count"), "reported_complete_jd_count": result.get("complete_jd_count"), "reported_incomplete_jd_count": result.get("incomplete_jd_count"), "advertised_total": result.get("advertised_total") if type(result.get("advertised_total")) is int and result["advertised_total"] >= 0 else None, "missing_jd_total": len(candidates) + len(excluded), "missing_jd_sampled": len(samples), "missing_jd_not_sampled": len(candidates) - len(samples), "missing_jd_excluded": len(excluded), "excluded_count": len(excluded)}
    return {"company": source["company"], "source_url": source["source_url"], "crawler": source["crawler"], "source_context": cohort_context, "started_at": started_at, "completed_at": _now(), "elapsed_seconds": round(time.monotonic() - started, 3), "classification": _classification(result, jobs, errors), "pagination": _pagination(result), "counts": counts, "termination": termination, "errors": errors, "previous_jd_errors": list(result.get("previous_jd_errors") or []), "sources": _observed_sources(result, source["source_url"]), "jd_samples": samples, "jd_inventory": inventory, "replay": replay, "list_fetch": "skipped_reused_artifact" if replay else "isolated_worker", "_job_artifact": {"schema": SCHEMA, "company": source["company"], "source_url": source["source_url"], "sources": _observed_sources(result, source["source_url"]), "jobs": safe_jobs, "read_only": True, "model_calls": 0, "database_writes": 0, "config_writes": 0}, "read_only": True, "model_calls": 0, "database_writes": 0, "config_writes": 0}


def code_fingerprint_files() -> list[str]:
    paths = [Path(__file__), ROOT / "packages/pipeline/isolation.py", ROOT / "packages/recruitment_core/entry_crawl.py", ROOT / "packages/recruitment_core/runner.py", ROOT / "packages/recruitment_core/job_details.py"] + sorted((ROOT / "packages/recruitment_core/crawlers").glob("*.py"))
    return [str(path.relative_to(ROOT)) for path in paths]


def code_fingerprint() -> str:
    digest = hashlib.sha256()
    for name in code_fingerprint_files():
        digest.update(name.encode()); digest.update(b"\0"); digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def run_verification(sources: list[dict[str, object]], output: Path, *, concurrency: int = 2, timeout_seconds: float = 90.0, inputs: Mapping[str, object] | None = None, replay_rows: Mapping[str, Mapping[str, object]] | None = None) -> dict[str, object]:
    if not 1 <= concurrency <= MAX_WORKERS or timeout_seconds <= 0 or not 0 < len(sources) <= MAX_COMPANIES:
        raise ValueError("scope/concurrency/timeout is outside bounded verifier limits")
    started_at, checkpoint_path = _now(), output / "checkpoint.json"; checkpoint = {"schema": SCHEMA, "status": "running", "started_at": started_at, "companies": [x["company"] for x in sources], "concurrency": concurrency, "completed": [], "last_completed": None}; _write(checkpoint_path, checkpoint)
    by_name = {}
    with ThreadPoolExecutor(max_workers=min(concurrency, len(sources))) as pool:
        futures = {pool.submit(verify_company, source, timeout_seconds=timeout_seconds, frozen_result=(replay_rows or {}).get(source["company"])): source for source in sources}
        for future in as_completed(futures):
            row = future.result(); artifact = row.pop("_job_artifact", {"schema": SCHEMA, "company": row["company"], "jobs": [], "read_only": True}); artifact_path = Path("companies") / f"{_hash(str(row['company']))[:16]}.json"; _write(output / artifact_path, artifact); row["job_artifact"] = artifact_path.as_posix(); by_name[row["company"]] = row; checkpoint["completed"].append(row["company"]); checkpoint["last_completed"] = {"company": row["company"], "completed_at": row["completed_at"], "classification": row["classification"], "pagination_state": row["pagination"]["pagination_state"], "counts": row["counts"], "job_artifact": row["job_artifact"]}; _write(checkpoint_path, checkpoint)
            print(json.dumps({"event": "company_complete", "company": row["company"], "classification": row["classification"], "pagination_state": row["pagination"]["pagination_state"], "counts": row["counts"]}, ensure_ascii=False), flush=True)
    results = [by_name[x["company"]] for x in sources]
    counts = {"companies": len(results), "observed_jobs": sum(x["counts"]["observed_jobs"] for x in results), "missing_jd": sum(x["counts"]["missing_jd_total"] for x in results), "jd_samples": sum(x["counts"]["missing_jd_sampled"] for x in results), "excluded": sum(x["counts"]["excluded_count"] for x in results), "classification": dict(sorted(Counter(x["classification"] for x in results).items())), "pagination_state": dict(sorted(Counter(x["pagination"]["pagination_state"] for x in results).items())), "termination": dict(sorted(Counter(code for x in results for code in x["termination"]).items())), "errors": dict(sorted(Counter(error["code"] for x in results for error in x["errors"]).items()))}
    report = {"schema": SCHEMA, "scope": {"name": "page_repair_20260906", "companies_total": MAX_COMPANIES, "companies_selected": [x["company"] for x in sources], "concurrency": concurrency, "per_company_list_timeout_seconds": timeout_seconds, "max_missing_jd_samples_per_company": MAX_SAMPLES, "list_only_plus_missing_jd_samples": True, "sample_nonall": True, "replay_only": bool(replay_rows)}, "read_only": True, "model_calls": 0, "database_writes": 0, "config_writes": 0, "modelcalls": 0, "dbwrites": 0, "configwrites": 0, "started_at": started_at, "completed_at": _now(), "code_fingerprint": code_fingerprint(), "code_fingerprint_files": code_fingerprint_files(), "inputs": dict(inputs or {}), "counts": counts, "sources": sources, "results": results}
    _write(output / "report.json", report); checkpoint.update(status="completed", completed_at=report["completed_at"]); _write(checkpoint_path, checkpoint); return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--company", action="append", dest="companies"); parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2); parser.add_argument("--timeout-seconds", type=float, default=90.0); parser.add_argument("--output", type=Path, default=OUT); parser.add_argument("--diagnosis", type=Path, default=DIAG); parser.add_argument("--baseline", type=Path, default=BASE); parser.add_argument("--round4", type=Path, default=ROUND4); parser.add_argument("--replay-dir", type=Path, help="reuse a prior report's per-company job artifacts; skips list crawling"); parser.add_argument("--dry-run", action="store_true"); args = parser.parse_args(argv)
    if args.timeout_seconds <= 0: parser.error("--timeout-seconds must be positive")
    try:
        sources = select_sources(resolve_sources(args.diagnosis, args.baseline, args.round4), args.companies)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if args.dry_run:
        print(json.dumps({"schema": SCHEMA, "dry_run": True, "scope": {"companies_total": MAX_COMPANIES, "companies_selected": [x["company"] for x in sources], "concurrency": args.concurrency, "per_company_list_timeout_seconds": args.timeout_seconds, "max_missing_jd_samples_per_company": MAX_SAMPLES, "replay_only": bool(args.replay_dir)}, "replay_dir": str(args.replay_dir) if args.replay_dir else None, "will_start_isolated_workers": False, "will_call_business_api": False, "will_touch_database": False, "will_write_config": False, "sources": sources}, ensure_ascii=False, indent=2)); return 0
    output, eval_root = args.output.resolve(), (ROOT / ".data/evals").resolve()
    if args.replay_dir and output == OUT.resolve():
        output = OUT / "replay"
    if not output.is_relative_to(eval_root): parser.error("--output must remain below .data/evals")
    if output.exists() and any(output.iterdir()): parser.error("output directory is non-empty; preserve prior evidence")
    inputs = {"diagnosis": {"path": "docs/CRAWL_PAGE_DIAGNOSIS_20260906.md", "sha256": _hash(args.diagnosis.read_text(encoding="utf-8"))}, "baseline": {"path": str(args.baseline), "sha256": _hash(args.baseline.read_text(encoding="utf-8"))}, "round4": {"path": str(args.round4), "sha256": _hash(args.round4.read_text(encoding="utf-8")) if args.round4.exists() else None}}
    replay_rows = None
    if args.replay_dir:
        try:
            replay_rows = load_replay(args.replay_dir, sources)
            replay_report = args.replay_dir.resolve() / "report.json"
            inputs["replay_dir"] = str(args.replay_dir.resolve())
            inputs["replay_report_sha256"] = _hash(replay_report.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
    report = run_verification(sources, output, concurrency=args.concurrency, timeout_seconds=args.timeout_seconds, inputs=inputs, replay_rows=replay_rows); print(json.dumps({"report": str(output / "report.json"), "counts": report["counts"]}, ensure_ascii=False)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
