"""Hydrate every eligible OfferBiu full-crawl job from frozen checkpoints."""

from __future__ import annotations

import argparse
from collections import Counter, deque
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.domain.job_identity import normalize_job_identity_url
from packages.pipeline.isolation import fetch_job_detail_result_isolated
from packages.recruitment_core.jd_capture import assess_jd_capture
from packages.recruitment_core.job_details import _url_job_id


MAX_WORKERS = 12
MAX_TIMEOUT_SECONDS = 45.0
DEFAULT_TIMEOUT_SECONDS = 45.0
SUMMARY_WRITE_EVERY = 32
SUMMARY_WRITE_INTERVAL_SECONDS = 1.0
OUTCOMES = {
    "complete",
    "hydrated",
    "failed",
    "cohort_unconfirmed_not_hydrated",
    "pending",
}
_NATIVE_ID_FIELDS = (
    "native_job_id",
    "nativeID",
    "nativeId",
    "native_id",
    "source_job_id",
    "sourceJobId",
    "source_post_id",
    "sourcePostId",
    "post_id",
    "postId",
    "position_id",
    "positionId",
    "job_id",
    "jobId",
    "external_id",
    "externalId",
    "id",
)
# Only fields explicitly named as native/source IDs can authorize cross-company reuse.
_EXPLICIT_NATIVE_ID_FIELDS = (
    "native_job_id",
    "nativeID",
    "nativeId",
    "native_id",
    "source_job_id",
    "sourceJobId",
    "source_post_id",
    "sourcePostId",
    "post_id",
    "postId",
    "position_id",
    "positionId",
)
_SHARING_STABLE_JOB_FIELDS = (
    "city",
    "job_type",
    "employment_type",
    "recruitment_track",
    "detail_interaction",
    "department",
    "job_family",
    "work_city",
    "location",
)
_COMPANY_EVIDENCE_PREFIXES = frozenset(
    {
        "company",
        "company_id",
        "company_name",
        "employer",
        "employer_id",
        "employer_name",
        "organization",
        "organization_id",
        "organization_name",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _replace_with_retry(temporary: Path, path: Path) -> None:
    for attempt in range(7):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 6:
                raise
            sleep(min(0.1 * 2**attempt, 1.0))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    _replace_with_retry(temporary, path)


def _write_progress(path: Path, value: Any) -> None:
    try:
        _write_json(path, value)
    except PermissionError as exc:
        # Job checkpoints are authoritative; a locked progress display is not fatal.
        print(f"Progress update deferred; checkpoints retained: {exc}", file=sys.stderr, flush=True)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    _replace_with_retry(temporary, path)


def _require_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    eval_root = (ROOT / ".data" / "evals").resolve()
    if not resolved.is_relative_to(eval_root):
        raise ValueError("Output must stay under .data/evals")
    return resolved


def _checkpoint_files(checkpoint_dir: Path) -> list[Path]:
    checkpoint_dir = checkpoint_dir.resolve()
    if not checkpoint_dir.is_dir():
        raise ValueError(f"Checkpoint directory does not exist: {checkpoint_dir}")
    return sorted(path for path in checkpoint_dir.glob("*.json") if path.is_file())


def _input_fingerprint(paths: list[Path]) -> str:
    entries = [
        {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in paths
    ]
    return _sha256(entries)


def _cohort_confirmed(job: Mapping[str, Any]) -> bool:
    try:
        cohort = int(job.get("cohort") or 0)
    except (TypeError, ValueError):
        cohort = 0
    return cohort == 2027 and str(job.get("cohort_status") or "").strip().casefold() == "confirmed"


def _nonempty_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _native_id(job: Mapping[str, Any]) -> str:
    for field in _NATIVE_ID_FIELDS:
        value = _nonempty_text(job.get(field))
        if value:
            return value
    return ""


def _explicit_native_id(job: Mapping[str, Any]) -> tuple[str, str]:
    """Return a source-native ID field, never the generic/synthetic ``id``."""

    for field in _EXPLICIT_NATIVE_ID_FIELDS:
        value = _nonempty_text(job.get(field))
        if value:
            return field, value
    return "", ""


def _identity_value_key(value: Any) -> str:
    return "".join(_nonempty_text(value).split()).casefold()


def _observed_url_native_id(job: Mapping[str, Any]) -> str:
    """Read an ID only from known official detail routes, never from a synthetic job key."""

    raw_url = _nonempty_text(job.get("detail_url") or job.get("jd_url"))
    normalized_url = normalize_job_identity_url(raw_url)
    if not normalized_url:
        return ""
    parsed = urlsplit(raw_url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    if host.endswith(".jobs.feishu.cn"):
        segments = [part for part in path.split("/") if part]
        try:
            position_index = segments.index("position")
        except ValueError:
            return ""
        if (
            position_index + 2 >= len(segments)
            or not segments[position_index + 1]
            or segments[position_index + 2] != "detail"
        ):
            return ""
    elif host == "hotjob.cn" or host.endswith(".hotjob.cn"):
        if not path.rstrip("/").endswith("/posdetail.html"):
            return ""
    else:
        return ""
    return _nonempty_text(_url_job_id(raw_url))


def _sharing_native_id(job: Mapping[str, Any]) -> str:
    """Return one non-synthetic ID, rejecting explicit/URL ID conflicts."""

    explicit_values = [
        _nonempty_text(job.get(field))
        for field in _EXPLICIT_NATIVE_ID_FIELDS
        if _nonempty_text(job.get(field))
    ]
    observed_url_id = _observed_url_native_id(job)
    candidates = [*explicit_values, observed_url_id] if observed_url_id else explicit_values
    if not candidates:
        return ""
    if len({_identity_value_key(value) for value in candidates}) != 1:
        return ""
    return candidates[0]


def _identity_evidence_items(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _evidence_prefix(value: str) -> str:
    return value.split(":", 1)[0].strip().casefold().replace("-", "_")


def _is_company_prefix(value: str) -> bool:
    normalized = value.strip().casefold().replace("-", "_")
    return normalized in _COMPANY_EVIDENCE_PREFIXES or any(
        token in normalized for token in ("company", "employer", "organization")
    )


def _has_company_identity_evidence(value: Any) -> bool:
    """Reject evidence that says the company itself was part of verification."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized_key = str(key).strip().casefold().replace("-", "_")
            if _is_company_prefix(normalized_key) and _nonempty_text(item):
                return True
        return False
    return any(_is_company_prefix(_evidence_prefix(item)) for item in _identity_evidence_items(value))


def _has_company_field(value: Mapping[str, Any]) -> bool:
    for key, item in value.items():
        normalized_key = str(key).strip().casefold().replace("-", "_")
        if _is_company_prefix(normalized_key) and _nonempty_text(item):
            return True
    return False


def _company_key(company: str) -> str:
    return " ".join(company.split()).casefold()


def job_identity(company: str, job: Mapping[str, Any]) -> dict[str, str]:
    """Return a stable dedupe identity without weakening detail identity checks."""

    normalized_company = _company_key(company)
    raw_url = _nonempty_text(job.get("detail_url") or job.get("jd_url"))
    normalized_url = normalize_job_identity_url(raw_url)
    detail_host = urlsplit(normalized_url).netloc.casefold() if normalized_url else ""
    native_id = _native_id(job)
    if native_id:
        normalized_native_id = " ".join(native_id.split()).casefold()
        value = f"{normalized_company}:{detail_host or '<no-host>'}:{normalized_native_id}"
        identity_type = "company_native_id"
        identity_value = value
    else:
        if normalized_url:
            value = f"{normalized_company}:{normalized_url}"
            identity_type = "company_detail_url"
            identity_value = value
        else:
            digest = _sha256(dict(job))
            value = f"{normalized_company}:{digest}"
            identity_type = "company_job_hash"
            identity_value = value
    return {
        "type": identity_type,
        "value": identity_value,
        "job_key": hashlib.sha256(
            f"offerbiu-hydration-v1\0{identity_type}\0{identity_value}".encode("utf-8")
        ).hexdigest(),
    }


def _assessment_payload(job: Mapping[str, Any]) -> dict[str, Any]:
    assessment = assess_jd_capture(job)
    return {
        "complete": assessment.complete,
        "reason_code": assessment.reason_code,
        "reason": assessment.reason,
    }


def _source_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "checkpoint": record["source_checkpoint"],
        "source_index": record["source_index"],
        "crawl_url": record["crawl_url"],
        "input_sha256": record["input_sha256"],
    }


def _source_labels(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "company": record["company"],
        "crawl_url": record["crawl_url"],
        "source_checkpoint": record["source_checkpoint"],
        "source_index": record["source_index"],
    }


def _request_job(record: Mapping[str, Any]) -> dict[str, Any]:
    request_job = deepcopy(record["job"])
    request_job.setdefault("company", record["company"])
    if record["crawl_url"].startswith(("http://", "https://")):
        request_job.setdefault("careers_url", record["crawl_url"])
        request_job.setdefault("source_list_url", record["crawl_url"])
    return request_job


def _record(
    checkpoint: Path,
    source_index: int,
    company: str,
    crawl_url: str,
    job: Mapping[str, Any],
) -> dict[str, Any]:
    from packages.recruitment_core.offerbiu_policy import apply_offerbiu_cohort

    raw_job = apply_offerbiu_cohort(job)
    identity = job_identity(company, raw_job)
    input_sha256 = _sha256({"company": company, "crawl_url": crawl_url, "job": raw_job})
    record = {
        "job_key": identity["job_key"],
        "identity": identity,
        "company": company,
        "crawl_url": crawl_url,
        "source_checkpoint": str(checkpoint.resolve()),
        "source_index": source_index,
        "input_sha256": input_sha256,
        "job": deepcopy(raw_job),
    }
    record["duplicate_count"] = 1
    record["duplicate_sources"] = [_source_entry(record)]
    return record


def load_checkpoint_jobs(checkpoint_dir: Path) -> list[dict[str, Any]]:
    """Load the single-result checkpoint format emitted by evaluate_sample."""

    records: list[dict[str, Any]] = []
    for path in _checkpoint_files(Path(checkpoint_dir)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid checkpoint JSON: {path}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"Checkpoint must be an object: {path}")
        if "samples" in payload:
            raise ValueError(f"Checkpoint has the old samples-array shape: {path}")
        if "crawl" not in payload:
            raise ValueError(f"Checkpoint requires a top-level crawl object: {path}")

        crawl = payload.get("crawl")
        if crawl is None:
            continue
        if not isinstance(crawl, Mapping) or not isinstance(crawl.get("raw_jobs"), list):
            raise ValueError(f"Checkpoint requires crawl.raw_jobs: {path}")
        company = _nonempty_text(payload.get("company")) or "unknown-company"
        crawl_url = _nonempty_text(payload.get("crawl_url"))
        for source_index, raw_job in enumerate(crawl["raw_jobs"]):
            if not isinstance(raw_job, Mapping):
                raise ValueError(f"Checkpoint raw_jobs must contain objects: {path}:{source_index}")
            records.append(_record(path, source_index, company, crawl_url, raw_job))
    return records


def _representative_priority(record: Mapping[str, Any]) -> tuple[int, int]:
    job = record["job"]
    return (
        int(_cohort_confirmed(job)),
        int(assess_jd_capture(job).complete),
    )


def deduplicate_jobs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by company/native ID, then company/detail URL or exact hash."""

    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for incoming in records:
        key = incoming["job_key"]
        current = grouped.get(key)
        if current is None:
            grouped[key] = incoming
            order.append(key)
            continue

        sources = current["duplicate_sources"]
        sources.append(_source_entry(incoming))
        chosen = incoming if _representative_priority(incoming) > _representative_priority(current) else current
        chosen["duplicate_count"] = len(sources)
        chosen["duplicate_sources"] = sources
        grouped[key] = chosen
    return [grouped[key] for key in order]


def _base_row(record: Mapping[str, Any]) -> dict[str, Any]:
    job = record["job"]
    row: dict[str, Any] = {
        "job_key": record["job_key"],
        "identity": deepcopy(record["identity"]),
        "company": record["company"],
        "crawl_url": record["crawl_url"],
        "source_checkpoint": record["source_checkpoint"],
        "source_index": record["source_index"],
        "source_labels": _source_labels(record),
        "input_sha256": record["input_sha256"],
        "duplicate_count": record["duplicate_count"],
        "duplicate_sources": deepcopy(record["duplicate_sources"]),
        "job": deepcopy(job),
        "original_jd_raw": deepcopy(job.get("jd_raw")),
        "original_capture_evidence": deepcopy(job.get("capture_evidence")),
        "jd_raw": deepcopy(job.get("jd_raw")),
        "capture_evidence": deepcopy(job.get("capture_evidence")),
        "read_only": True,
        "model_calls": 0,
        "db_writes": 0,
    }
    for field in (
        "title",
        "city",
        "job_type",
        "detail_url",
        "jd_url",
        "native_job_id",
        "nativeID",
        "nativeId",
        "native_id",
        "source_job_id",
        "cohort",
        "cohort_status",
    ):
        if field in job:
            row[field] = deepcopy(job[field])
    return row


def _candidate_job(record: Mapping[str, Any], hydration: Mapping[str, Any]) -> dict[str, Any]:
    candidate = deepcopy(record["job"])
    candidate["jd_raw"] = hydration.get("detail")
    candidate["capture_evidence"] = hydration.get("capture_evidence")
    return candidate


def _share_group_key(record: Mapping[str, Any]) -> str | None:
    """Build a conservative reuse key from stable job identity/content fields."""

    job = record["job"]
    detail_url = normalize_job_identity_url(job.get("detail_url") or job.get("jd_url"))
    native_id = _sharing_native_id(job)
    title = _nonempty_text(job.get("title"))
    cohort_status = _nonempty_text(job.get("cohort_status"))
    original_detail = job.get("jd_raw") or ""
    raw_original_evidence = job.get("capture_evidence")
    if raw_original_evidence is None or raw_original_evidence == {}:
        original_evidence: Mapping[str, Any] = {}
    elif isinstance(raw_original_evidence, Mapping):
        original_evidence = dict(raw_original_evidence)
    else:
        return None
    if not all((detail_url, native_id, title, job.get("cohort"), cohort_status)):
        return None
    if (
        _has_company_identity_evidence(job.get("identity_evidence"))
        or _has_company_identity_evidence(original_evidence)
    ):
        return None

    stable_job: dict[str, Any] = {
        "detail_url": detail_url,
        "native_id": native_id,
        "title": title,
        "cohort": job.get("cohort"),
        "cohort_status": cohort_status,
        "jd_raw": original_detail,
        "capture_evidence": original_evidence,
    }
    for field in _SHARING_STABLE_JOB_FIELDS:
        if field in job:
            stable_job[field] = deepcopy(job[field])
    return _sha256(
        stable_job
    )


def _identity_evidence_values(hydration: Mapping[str, Any], prefix: str) -> list[str]:
    normalized_prefix = prefix.casefold()
    return [
        item.split(":", 1)[1].strip()
        for item in _identity_evidence_items(hydration.get("identity_evidence"))
        if ":" in item and _evidence_prefix(item) == normalized_prefix
    ]


def _shareable_hydration(record: Mapping[str, Any], hydration: Mapping[str, Any]) -> bool:
    """Allow reuse only after one response proves the same official native job."""

    if _nonempty_text(hydration.get("status")).casefold() != "complete":
        return False
    if _nonempty_text(hydration.get("identity_status")).casefold() != "matched":
        return False
    if _has_company_field(hydration) or _has_company_identity_evidence(hydration.get("identity_evidence")):
        return False

    capture_evidence = hydration.get("capture_evidence")
    if not isinstance(capture_evidence, Mapping):
        return False
    if _nonempty_text(capture_evidence.get("method")).casefold() != "official_api":
        return False
    if _has_company_identity_evidence(capture_evidence):
        return False

    detail_url = normalize_job_identity_url(record["job"].get("detail_url") or record["job"].get("jd_url"))
    capture_url = normalize_job_identity_url(capture_evidence.get("source_url"))
    if not detail_url or capture_url != detail_url:
        return False

    native_id = _sharing_native_id(record["job"])
    title = _nonempty_text(record["job"].get("title"))
    native_values = _identity_evidence_values(hydration, "native_id")
    title_values = _identity_evidence_values(hydration, "title")
    if not native_id or len(native_values) != 1 or len(title_values) != 1:
        return False
    if _identity_value_key(native_values[0]) != _identity_value_key(native_id) or title_values[0] != title:
        return False

    candidate_assessment = _assessment_payload(_candidate_job(record, hydration))
    return candidate_assessment["complete"]


def _finish_row(
    record: Mapping[str, Any],
    hydration: Mapping[str, Any],
    outcome: str,
    *,
    attempted: bool,
    original_assessment: Mapping[str, Any] | None = None,
    hydration_assessment: Mapping[str, Any] | None = None,
    failure_reason: str = "",
    hydration_reused_from: str | None = None,
    hydration_reuse_source_labels: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row = _base_row(record)
    hydration_value = deepcopy(dict(hydration))
    row.update(
        {
            "hydration": hydration_value,
            "hydration_outcome": outcome,
            "hydration_status": _nonempty_text(hydration_value.get("status")) or outcome,
            "request_made": attempted,
            "candidate_jd_raw": deepcopy(hydration_value.get("detail")),
            "candidate_capture_evidence": deepcopy(hydration_value.get("capture_evidence")),
            "original_capture_assessment": deepcopy(original_assessment),
            "hydration_capture_assessment": deepcopy(hydration_assessment),
            "failure_reason": failure_reason,
            "hydration_reused_from": hydration_reused_from,
            "hydration_reuse_source_labels": deepcopy(hydration_reuse_source_labels),
            "finished_at": _now(),
        }
    )
    if outcome in {"complete", "hydrated"}:
        detail = hydration_value.get("detail")
        capture_evidence = hydration_value.get("capture_evidence")
        if detail is None:
            detail = row["jd_raw"]
        if capture_evidence is None:
            capture_evidence = row["capture_evidence"]
        row["job"]["jd_raw"] = deepcopy(detail)
        row["job"]["capture_evidence"] = deepcopy(capture_evidence)
        row["jd_raw"] = deepcopy(detail)
        row["capture_evidence"] = deepcopy(capture_evidence)
    return row


def _prepare_without_request(record: Mapping[str, Any]) -> dict[str, Any] | None:
    job = record["job"]
    original_assessment = _assessment_payload(job)
    if not _cohort_confirmed(job):
        return _finish_row(
            record,
            {
                "status": "skipped_cohort_unconfirmed",
                "detail": "",
                "request_made": False,
                "skip_reason": "cohort_unconfirmed",
            },
            "cohort_unconfirmed_not_hydrated",
            attempted=False,
            original_assessment=original_assessment,
        )
    if original_assessment["complete"]:
        return _finish_row(
            record,
            {
                "status": "skipped_existing_complete",
                "detail": str(job.get("jd_raw") or ""),
                "capture_evidence": deepcopy(job.get("capture_evidence")),
                "detail_url": _nonempty_text(job.get("detail_url") or job.get("jd_url")),
                "request_made": False,
                "skip_reason": "capture_already_complete",
            },
            "complete",
            attempted=False,
            original_assessment=original_assessment,
            hydration_assessment=original_assessment,
        )
    return None


def _failed_hydration(error: BaseException) -> dict[str, Any]:
    return {
        "detail": "",
        "status": "fetch_failed",
        "error_type": type(error).__name__,
        "error": str(error)[-1_000:],
        "attempts": [],
    }


def _hydrate_record(
    record: Mapping[str, Any],
    timeout: float,
    fetch: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    original_job = record["job"]
    original_assessment = _assessment_payload(original_job)
    request_job = _request_job(record)

    started = monotonic()
    try:
        returned = fetch(request_job, timeout_seconds=timeout)
    except Exception as exc:  # one failed detail must not stop the batch
        hydration = _failed_hydration(exc)
        hydration["elapsed_seconds"] = monotonic() - started
        return _finish_row(
            record,
            hydration,
            "failed",
            attempted=True,
            original_assessment=original_assessment,
            failure_reason=f"fetch:{type(exc).__name__}",
        )

    if not isinstance(returned, Mapping):
        hydration = {
            "detail": "",
            "status": "invalid_response",
            "error_type": "InvalidHydrationResponse",
            "attempts": [],
        }
        return _finish_row(
            record,
            hydration,
            "failed",
            attempted=True,
            original_assessment=original_assessment,
            failure_reason="fetch:invalid_response",
        )

    hydration = dict(returned)
    hydration["elapsed_seconds"] = monotonic() - started
    reuse = hydration.get("_detail_reuse")
    attempted = not (isinstance(reuse, Mapping) and reuse.get("request_made") is False)
    candidate = _candidate_job(record, hydration)
    candidate_assessment = _assessment_payload(candidate)
    returned_status = _nonempty_text(hydration.get("status")).casefold()
    if returned_status == "complete" and candidate_assessment["complete"]:
        return _finish_row(
            record,
            hydration,
            "hydrated",
            attempted=attempted,
            original_assessment=original_assessment,
            hydration_assessment=candidate_assessment,
        )

    failure_reason = (
        f"status:{returned_status or 'missing'}"
        if returned_status != "complete"
        else f"capture:{candidate_assessment['reason_code']}"
    )
    return _finish_row(
        record,
        hydration,
        "failed",
        attempted=attempted,
        original_assessment=original_assessment,
        hydration_assessment=candidate_assessment,
        failure_reason=failure_reason,
    )


def _reuse_hydration(
    record: Mapping[str, Any],
    representative: Mapping[str, Any],
    representative_result: Mapping[str, Any],
) -> dict[str, Any]:
    hydration = deepcopy(dict(representative_result["hydration"]))
    candidate_assessment = _assessment_payload(_candidate_job(record, hydration))
    return _finish_row(
        record,
        hydration,
        "hydrated",
        attempted=False,
        original_assessment=_assessment_payload(record["job"]),
        hydration_assessment=candidate_assessment,
        hydration_reused_from=representative["job_key"],
        hydration_reuse_source_labels=_source_labels(representative),
    )


def _load_saved_checkpoint(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid hydration checkpoint: {path}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"Hydration checkpoint must be an object: {path}")
    if value.get("job_key") != record["job_key"]:
        raise ValueError(f"Hydration checkpoint identity mismatch: {path}")
    if value.get("input_sha256") != record["input_sha256"]:
        raise ValueError(f"Hydration checkpoint input hash mismatch: {path}")
    if value.get("hydration_outcome") not in OUTCOMES - {"pending"}:
        raise ValueError(f"Hydration checkpoint is not terminal: {path}")
    return dict(value)


def _write_jobs(path: Path, records: list[dict[str, Any]], results: Mapping[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            value = results[record["job_key"]]
            handle.write(json.dumps(value, ensure_ascii=False, default=str))
            handle.write("\n")
    _replace_with_retry(temporary, path)


def _result_outcome(value: Mapping[str, Any] | None) -> str:
    return str((value or {}).get("hydration_outcome") or "pending")


def _result_counts(
    records: list[dict[str, Any]],
    results: Mapping[str, dict[str, Any]],
) -> tuple[Counter[str], int]:
    counts: Counter[str] = Counter()
    attempted = 0
    for record in records:
        value = results.get(record["job_key"])
        counts[_result_outcome(value)] += 1
        attempted += bool(value and value.get("request_made"))
    return counts, attempted


def _summary(
    manifest: Mapping[str, Any],
    records: list[dict[str, Any]],
    results: Mapping[str, dict[str, Any]],
    *,
    status_counts: Mapping[str, int] | None = None,
    attempted_job_count: int | None = None,
) -> dict[str, Any]:
    if status_counts is None or attempted_job_count is None:
        status_counts, attempted_job_count = _result_counts(records, results)
    counts = Counter(status_counts)
    finished = len(records) - counts.get("pending", 0)
    stage_complete = finished == len(records)
    complete = counts.get("complete", 0)
    hydrated = counts.get("hydrated", 0)
    failed = counts.get("failed", 0)
    unconfirmed = counts.get("cohort_unconfirmed_not_hydrated", 0)
    return {
        **manifest,
        "unique_job_count": len(records),
        "finished_job_count": finished,
        "remaining_job_count": len(records) - finished,
        "stage_complete": stage_complete,
        "all_attempts_finished": stage_complete,
        "status": "complete" if stage_complete else "in_progress",
        "status_counts": dict(counts),
        "counts": {
            "complete": complete,
            "hydrated": hydrated,
            "failed": failed,
            "cohort_unconfirmed_not_hydrated": unconfirmed,
        },
        "complete": complete,
        "hydrated": hydrated,
        "failed": failed,
        "cohort_unconfirmed_not_hydrated": unconfirmed,
        "unconfirmed_cohort_not_hydrated": unconfirmed,
        "already_complete_count": complete,
        "hydrated_count": hydrated,
        "failed_count": failed,
        "cohort_unconfirmed_not_hydrated_count": unconfirmed,
        "attempted_job_count": attempted_job_count,
        "updated_at": _now(),
    }


def _validate_options(workers: int, timeout: float) -> None:
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be 1..{MAX_WORKERS}")
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must be greater than 0 and at most 45 seconds")


def run(
    checkpoint_dir: Path,
    output_dir: Path,
    *,
    workers: int = MAX_WORKERS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    resume: bool = False,
    fetch: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Hydrate a complete checkpoint directory and return the final summary."""

    _validate_options(workers, timeout)
    output = _require_output_dir(Path(output_dir))
    input_dir = Path(checkpoint_dir).resolve()
    paths = _checkpoint_files(input_dir)
    input_fingerprint = _input_fingerprint(paths)
    records = deduplicate_jobs(load_checkpoint_jobs(input_dir))
    checkpoint_output = output / "checkpoints"
    checkpoint_output.mkdir(parents=True, exist_ok=True)

    manifest_path = output / "manifest.json"
    manifest = {
        "source": "offerbiu",
        "phase": "full_crawl_hydration",
        "checkpoint_dir": str(input_dir),
        "input_fingerprint": input_fingerprint,
        "input_checkpoint_count": len(paths),
        "raw_job_count": sum(record["duplicate_count"] for record in records),
        "deduplicated_job_count": len(records),
        "workers": workers,
        "timeout_seconds_per_job": timeout,
        "read_only": True,
        "model_calls": 0,
        "db_writes": 0,
        "started_at": _now(),
    }
    if manifest_path.exists():
        if not resume:
            raise ValueError("Existing hydration run requires --resume")
        old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old_manifest.get("input_fingerprint") != input_fingerprint:
            raise ValueError("Existing hydration run has a different checkpoint input")
        manifest["started_at"] = old_manifest.get("started_at") or manifest["started_at"]
    _write_json(manifest_path, manifest)

    results: dict[str, dict[str, Any]] = {}
    if resume:
        for record in records:
            saved_path = checkpoint_output / f"{record['job_key']}.json"
            if saved_path.exists():
                results[record["job_key"]] = _load_saved_checkpoint(saved_path, record)

    for record in records:
        key = record["job_key"]
        if key in results:
            continue
        prepared = _prepare_without_request(record)
        if prepared is not None:
            results[key] = prepared
            _write_json(checkpoint_output / f"{key}.json", prepared)

    status_counts, attempted_job_count = _result_counts(records, results)
    _write_json(
        output / "summary.json",
        _summary(
            manifest,
            records,
            results,
            status_counts=status_counts,
            attempted_job_count=attempted_job_count,
        ),
    )
    last_summary_write = monotonic()
    completed_since_summary = 0

    from packages.recruitment_core.detail_reuse import ReusingDetailHydrator

    hydrate = ReusingDetailHydrator(fetch or fetch_job_detail_result_isolated)
    pending = [record for record in records if record["job_key"] not in results]
    pending_group_keys: dict[str, str | None] = {}
    group_members: dict[str, list[dict[str, Any]]] = {}
    for record in pending:
        group_key = _share_group_key(record)
        pending_group_keys[record["job_key"]] = group_key
        if group_key is not None:
            group_members.setdefault(group_key, []).append(record)

    work_queue = deque()
    reuse_waiters: dict[str, list[dict[str, Any]]] = {}
    for record in pending:
        group_key = pending_group_keys[record["job_key"]]
        members = group_members.get(group_key or "", [])
        if group_key is None or len(members) == 1 or record["job_key"] == members[0]["job_key"]:
            work_queue.append(record)
        elif members[0]["job_key"] not in reuse_waiters:
            reuse_waiters[members[0]["job_key"]] = members[1:]

    def store_result(record: Mapping[str, Any], result: dict[str, Any]) -> None:
        nonlocal attempted_job_count, completed_since_summary
        previous = results.get(record["job_key"])
        previous_outcome = _result_outcome(previous)
        status_counts[previous_outcome] -= 1
        if status_counts[previous_outcome] <= 0:
            del status_counts[previous_outcome]
        attempted_job_count -= bool(previous and previous.get("request_made"))
        results[record["job_key"]] = result
        status_counts[_result_outcome(result)] += 1
        attempted_job_count += bool(result.get("request_made"))
        completed_since_summary += 1
        _write_json(checkpoint_output / f"{record['job_key']}.json", result)

    in_flight: dict[Any, dict[str, Any]] = {}
    with hydrate, ThreadPoolExecutor(max_workers=workers, thread_name_prefix="offerbiu-hydrate") as executor:
        while True:
            while len(in_flight) < workers and work_queue:
                record = work_queue.popleft()
                future = executor.submit(_hydrate_record, record, timeout, hydrate)
                in_flight[future] = record
            if not in_flight:
                break
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                record = in_flight.pop(future)
                try:
                    result = future.result()
                except Exception as exc:  # defensive boundary around one worker
                    result = _finish_row(
                        record,
                        _failed_hydration(exc),
                        "failed",
                        attempted=True,
                        original_assessment=_assessment_payload(record["job"]),
                        failure_reason=f"worker:{type(exc).__name__}",
                    )
                store_result(record, result)

                waiters = reuse_waiters.pop(record["job_key"], [])
                hydration = result.get("hydration")
                if isinstance(hydration, Mapping) and _shareable_hydration(record, hydration):
                    for waiter in waiters:
                        if _shareable_hydration(waiter, hydration):
                            store_result(waiter, _reuse_hydration(record=waiter, representative=record, representative_result=result))
                        else:
                            work_queue.append(waiter)
                else:
                    # A failed or weakly evidenced representative never auto-confirms its peers.
                    work_queue.extend(waiters)
            now = monotonic()
            if completed_since_summary and (
                completed_since_summary >= SUMMARY_WRITE_EVERY
                or now - last_summary_write >= SUMMARY_WRITE_INTERVAL_SECONDS
            ):
                _write_progress(
                    output / "summary.json",
                    _summary(
                        manifest,
                        records,
                        results,
                        status_counts=status_counts,
                        attempted_job_count=attempted_job_count,
                    ),
                )
                completed_since_summary = 0
                last_summary_write = now

    final_summary = _summary(
        manifest,
        records,
        results,
        status_counts=status_counts,
        attempted_job_count=attempted_job_count,
    )
    _write_jobs(output / "jobs.jsonl", records, results)
    _write_json(output / "summary.json", final_summary)
    return final_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, MAX_WORKERS + 1), default=MAX_WORKERS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        summary = run(
            args.checkpoint_dir,
            args.output_dir,
            workers=args.workers,
            timeout=args.timeout,
            resume=args.resume,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
