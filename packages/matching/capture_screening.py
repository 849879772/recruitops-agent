"""Deterministic offline screening and read-only snapshot reconciliation.

This module intentionally does not import the matching service, a repository, a
model client, or an HTTP client.  It reuses the existing rule functions only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlsplit

from packages.discovery.reconciliation import normalize_company_name, source_identity_for_url
from packages.domain.job_identity import normalize_job_identity_url, normalize_job_title
from packages.recruitment_core.detail_reuse import build_detail_reuse_key
from packages.recruitment_core.job_details import _is_reproducible_synthetic_id
from packages.matching.rules import (
    classify_job_directions,
    content_fingerprint,
    profile_fingerprint,
    screen_job,
)
from packages.recruitment_core.jd_capture import assess_jd_capture


SCREENING_SCHEMA_VERSION = "offerbiu-capture-screening.v2"
RECONCILIATION_SCHEMA_VERSION = "offerbiu-capture-reconciliation.v2"

# Required collections are deliberately small.  Identity fields are preferred
# and may be absent in legacy snapshots, including snapshots without business_key.
SNAPSHOT_CONTRACT: dict[str, Any] = {
    "format": "JSON object",
    "required_top_level": ["companies", "jobs", "analyses"],
    "optional_top_level": ["application_audit", "read_only"],
    "minimum_fields": {
        "companies": ["id", "name"],
        "jobs": [
            "id",
            "company_id",
            "title",
            "detail_url",
            "native_job_id",
            "business_key",
            "jd_raw",
        ],
        "analyses": ["job_id", "analysis_status", "match_score"],
    },
    "legacy_note": (
        "native_job_id, business_key, source_tenant, and source_identity may be null or "
        "omitted in legacy rows; no title-only merge is performed"
    ),
    "identity_fields": [
        "official_tenant or source_tenant",
        "native_job_id",
        "detail_url",
        "business_key",
        "company aliases",
    ],
}

_IDENTITY_URL_FIELDS = ("detail_url", "jd_url", "normalized_detail_url")
_TENANT_FIELDS = (
    "official_tenant",
    "source_tenant",
    "tenant",
    "portal_tenant",
)
_NATIVE_ID_FIELDS = (
    "native_job_id",
    "native_id",
    "nativeID",
    "nativeId",
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
)
# Capture binding follows the hydration verifier's native namespace.  Catalog
# row IDs and generic external IDs are deliberately not receipt identity.
_CAPTURE_NATIVE_ID_FIELDS = (
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
_BUSINESS_KEY_FIELDS = ("business_key", "job_business_key")
_ANALYSIS_FINGERPRINT_FIELDS = ("content_fingerprint", "jd_fingerprint", "content_sha256")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _casefold(value: Any) -> str:
    return _text(value).casefold()


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _first(mapping: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_text(value)] if _text(value) else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(_text(item) for item in value if _text(item)))


def _sha256_text(value: Any) -> str:
    return sha256(_text(value).encode("utf-8")).hexdigest()


def _model_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _title_identity(value: Any) -> str:
    return " ".join(_text(value).casefold().split())


def _receipt_identity_values(evidence: Mapping[str, Any], fields: Iterable[str]) -> set[str]:
    values = {_casefold(evidence.get(field)) for field in fields if _text(evidence.get(field))}
    identity_evidence = evidence.get("identity_evidence")
    if isinstance(identity_evidence, (list, tuple)):
        prefixes = {
            field.casefold().replace("-", "_")
            for field in fields
        }
        for item in identity_evidence:
            text = _text(item)
            prefix, separator, value = text.partition(":")
            normalized_prefix = prefix.casefold().replace("-", "_")
            if separator and normalized_prefix in prefixes and _text(value):
                values.add(_casefold(value))
    return values


def _receipt_title_values(evidence: Mapping[str, Any]) -> set[str]:
    values = {
        _title_identity(evidence.get(field))
        for field in ("title", "job_title", "position_name")
        if _title_identity(evidence.get(field))
    }
    identity_evidence = evidence.get("identity_evidence")
    if isinstance(identity_evidence, (list, tuple)):
        for item in identity_evidence:
            text = _text(item)
            prefix, separator, value = text.partition(":")
            if separator and prefix.casefold().replace("-", "_") == "title":
                normalized = _title_identity(value)
                if normalized:
                    values.add(normalized)
    return values


def _capture_binding_reason(job: Mapping[str, Any]) -> tuple[str, str] | None:
    evidence = job.get("capture_evidence")
    if not isinstance(evidence, Mapping):
        return None

    expected_url = _text(job.get("detail_url") or job.get("jd_url"))
    receipt_url = _text(evidence.get("source_url"))
    if not expected_url or not receipt_url:
        return "capture_job_url_missing", "The capture receipt is not bound to a complete job detail URL."
    expected_normalized = normalize_job_identity_url(expected_url)
    receipt_normalized = normalize_job_identity_url(receipt_url)
    if expected_normalized and receipt_normalized:
        urls_match = expected_normalized == receipt_normalized
    else:
        urls_match = expected_url.casefold() == receipt_url.casefold()
    if not urls_match:
        return "capture_source_mismatch", "The capture receipt source URL does not match this job."

    job_native_ids = {
        _casefold(job.get(field))
        for field in _CAPTURE_NATIVE_ID_FIELDS
        if _text(job.get(field))
    }
    receipt_native_ids = _receipt_identity_values(evidence, _CAPTURE_NATIVE_ID_FIELDS)
    if len(job_native_ids) > 1:
        return "capture_native_id_conflict", "The candidate job contains contradictory native IDs."
    if len(receipt_native_ids) > 1:
        return "capture_native_id_conflict", "The capture receipt contains contradictory native IDs."
    if job_native_ids and receipt_native_ids and job_native_ids != receipt_native_ids:
        return "capture_native_id_mismatch", "The capture receipt native ID does not match this job."

    receipt_titles = _receipt_title_values(evidence)
    job_title = _title_identity(job.get("title"))
    if len(receipt_titles) > 1:
        return "capture_title_conflict", "The capture receipt contains contradictory titles."
    if job_title and receipt_titles and receipt_titles != {job_title}:
        return "capture_title_mismatch", "The capture receipt title does not match this job."
    return None


def _assessment_payload(job: Mapping[str, Any]) -> dict[str, Any]:
    # Pass the complete job through the existing assessor.  The binding check
    # below closes the gap for receipts that are valid in isolation but belong
    # to another URL/native ID/title.
    assessment = assess_jd_capture(job)
    binding_reason = _capture_binding_reason(job)
    complete = assessment.complete and binding_reason is None
    reason_code, reason = (
        binding_reason
        if binding_reason is not None
        else (assessment.reason_code, assessment.reason)
    )
    jd_raw = job.get("jd_raw", "")
    return {
        "complete": complete,
        "incomplete": not complete,
        "reason_code": reason_code,
        "reason": reason,
        "jd_length": len(_text(jd_raw)),
        "jd_sha256": _sha256_text(jd_raw) if _text(jd_raw) else None,
    }


def _evidence_copy(value: Any) -> dict[str, Any] | None:
    return deepcopy(dict(value)) if isinstance(value, Mapping) else None


def _original_capture_evidence(row: Mapping[str, Any], job: Mapping[str, Any]) -> Any:
    direct = job.get("capture_evidence")
    if isinstance(direct, Mapping):
        return direct
    for name in ("capture_evidence", "original_capture_evidence"):
        candidate = row.get(name)
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _effective_job(row: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_job = row.get("job")
    if not isinstance(raw_job, Mapping):
        raise ValueError("record.job must be an object")

    job = deepcopy(dict(raw_job))
    if "jd_raw" not in job and row.get("jd_raw") is not None:
        job["jd_raw"] = row.get("jd_raw")
    if "detail_url" not in job and row.get("jd_url"):
        job["detail_url"] = row.get("jd_url")

    original_jd = job.get("jd_raw", "")
    original_evidence = _original_capture_evidence(row, job)
    if original_evidence is not None and not isinstance(job.get("capture_evidence"), Mapping):
        job["capture_evidence"] = deepcopy(dict(original_evidence))

    candidate_jd = row.get("candidate_jd_raw") or ""
    candidate_evidence = row.get("candidate_capture_evidence")
    candidate_job = deepcopy(job)
    candidate_job["jd_raw"] = candidate_jd
    candidate_job["capture_evidence"] = deepcopy(candidate_evidence)
    candidate_assessment = _assessment_payload(candidate_job)
    original_assessment = _assessment_payload(job)
    hydration_outcome = _casefold(row.get("hydration_outcome"))
    hydration_status = _casefold(row.get("hydration_status"))
    hydration_succeeded = hydration_outcome in {"hydrated", "complete"} and hydration_status in {"", "complete"}
    candidate_used = candidate_assessment["complete"] and hydration_succeeded
    if candidate_assessment["complete"] and not hydration_succeeded:
        candidate_assessment = {
            **candidate_assessment,
            "complete": False,
            "incomplete": True,
            "reason_code": "hydration_outcome_not_success",
            "reason": "Candidate detail is not used unless hydration_outcome is hydrated or complete.",
        }
    if candidate_used:
        job["jd_raw"] = candidate_jd
        job["capture_evidence"] = deepcopy(dict(candidate_evidence))
        jd_source = "candidate_hydration"
    else:
        jd_source = "original"

    effective_evidence = job.get("capture_evidence")
    capture = {
        "jd_source": jd_source,
        "candidate_available": bool(_text(candidate_jd)),
        "hydration_outcome": hydration_outcome or None,
        "hydration_status": hydration_status or None,
        "hydration_succeeded": hydration_succeeded,
        "candidate_used": candidate_used,
        "original_assessment": original_assessment,
        "candidate_assessment": candidate_assessment,
        "effective_assessment": _assessment_payload(job),
        "original_capture_evidence": _evidence_copy(original_evidence),
        "candidate_capture_evidence": _evidence_copy(candidate_evidence),
        "effective_capture_evidence": _evidence_copy(effective_evidence),
    }
    return job, capture


def _record_source(row: Mapping[str, Any], job: Mapping[str, Any], line_number: int) -> dict[str, Any]:
    identity = row.get("identity")
    return {
        "job_key": _text(row.get("job_key")),
        "identity": deepcopy(identity) if isinstance(identity, Mapping) else _text(identity) or None,
        "company": _text(row.get("company") or job.get("company") or job.get("company_name")),
        "crawl_url": _text(row.get("crawl_url")),
        "source_checkpoint": _text(row.get("source_checkpoint")),
        "source_index": row.get("source_index"),
        "source_labels": deepcopy(row.get("source_labels")) if row.get("source_labels") else [],
        "duplicate_count": row.get("duplicate_count", 0),
        "duplicate_sources": deepcopy(row.get("duplicate_sources")) if row.get("duplicate_sources") else [],
        "hydration_outcome": _text(row.get("hydration_outcome")) or None,
        "hydration_status": _text(row.get("hydration_status")) or None,
        "request_made": row.get("request_made"),
        "failure_reason": _text(row.get("failure_reason")) or None,
        "input_line": line_number,
    }


def _job_summary(job: Mapping[str, Any]) -> dict[str, Any]:
    jd_raw = job.get("jd_raw", "")
    return {
        "company": _text(job.get("company") or job.get("company_name")),
        "title": _text(job.get("title")),
        "city": _text(job.get("city")),
        "job_type": _text(job.get("job_type")),
        "detail_url": _text(job.get("detail_url") or job.get("jd_url")),
        "native_job_id": _text(_first(job, _NATIVE_ID_FIELDS)),
        "business_key": _text(_first(job, _BUSINESS_KEY_FIELDS)),
        "cohort": job.get("cohort"),
        "cohort_status": _text(job.get("cohort_status")),
        "jd_length": len(_text(jd_raw)),
        "jd_sha256": _sha256_text(jd_raw) if _text(jd_raw) else None,
    }


def _deferred_direction(result: Any) -> bool:
    status = _casefold(getattr(getattr(result, "analysis_status", None), "value", ""))
    if status != "direction_out":
        return False
    evidence = getattr(result, "evidence", ())
    return any(_casefold(getattr(item, "signal", "")) == "direction_evidence_missing" for item in evidence)


def _status_for_screening(result: Any) -> str:
    if result.eligible:
        return "passed"
    status = _casefold(getattr(getattr(result, "analysis_status", None), "value", ""))
    if status in {"cohort_unconfirmed", "jd_incomplete"} or _deferred_direction(result):
        return "deferred"
    return "excluded"


def _capture_id(row: Mapping[str, Any], line_number: int) -> str:
    supplied = _text(row.get("capture_id"))
    return supplied or f"offerbiu-{line_number:06d}"


def screen_capture_record(
    row: Mapping[str, Any],
    profile: Any,
    *,
    line_number: int = 1,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Screen one frozen hydration row and return (state, passed_record).

    The state record never includes full JD text.  The passed record contains
    the effective job so it can be handed to a later scoring process.
    """

    if not isinstance(row, Mapping):
        raise ValueError("JSONL record must be an object")
    capture_id = _capture_id(row, line_number)
    raw_job = row.get("job")
    if not isinstance(raw_job, Mapping):
        state = {
            "schema_version": SCREENING_SCHEMA_VERSION,
            "capture_id": capture_id,
            "source": _record_source(row, {}, line_number),
            "job": {},
            "status": "deferred",
            "rule_status": "input_schema_invalid",
            "reasons": ["input_schema_invalid:record.job must be an object"],
            "direction_classification": None,
            "screening_result": None,
            "capture": None,
        }
        return state, None

    job, capture = _effective_job(row)
    source = _record_source(row, job, line_number)
    try:
        classification = classify_job_directions(job)
        result = screen_job(job, profile)
        status = _status_for_screening(result)
        screening_result = _model_dump(result)
        direction_result = _model_dump(classification)
        reasons = list(result.reasons)
        if status == "deferred" and _deferred_direction(result):
            reasons.append("direction_unclear")
        rule_status = _casefold(getattr(getattr(result, "analysis_status", None), "value", ""))
    except Exception as exc:  # malformed frozen input must remain auditable, never pass
        status = "deferred"
        rule_status = "screening_error"
        reasons = [f"screening_error:{type(exc).__name__}:{_text(exc)}"]
        screening_result = None
        direction_result = None

    state: dict[str, Any] = {
        "schema_version": SCREENING_SCHEMA_VERSION,
        "capture_id": capture_id,
        "source": source,
        "job": _job_summary(job),
        "status": status,
        "rule_status": rule_status,
        "reasons": reasons,
        "direction_classification": direction_result,
        "screening_result": screening_result,
        "capture": capture,
    }
    if status != "passed":
        return state, None

    passed = deepcopy(state)
    passed["job"] = deepcopy(job)
    passed["screening"] = {
        "status": status,
        "rule_status": rule_status,
        "reasons": reasons,
        "capture": deepcopy(capture),
        "direction_classification": direction_result,
        "screening_result": screening_result,
    }
    return state, passed


def _atomic_jsonl_writer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    handle = temporary.open("w", encoding="utf-8", newline="\n")

    class _Writer:
        def __enter__(self):
            return self

        def write(self, value: Mapping[str, Any]) -> None:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")

        def __exit__(self, exc_type, exc, tb):
            handle.close()
            if exc_type is None:
                temporary.replace(path)
            else:
                temporary.unlink(missing_ok=True)

    return _Writer()


def _atomic_json_writer(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def screen_capture_jsonl(
    input_path: str | Path,
    output_dir: str | Path,
    profile: Any,
) -> dict[str, Any]:
    """Stream a frozen capture JSONL into audit state and passed-only files."""

    input_path = Path(input_path)
    output_dir = Path(output_dir)
    state_path = output_dir / "screening.jsonl"
    passed_path = output_dir / "passed-only.jsonl"
    summary_path = output_dir / "screening-report.json"
    counts: Counter[str] = Counter()
    rule_counts: Counter[str] = Counter()
    malformed_lines = 0
    blank_lines = 0
    candidate_used = 0
    candidate_available = 0
    records_read = 0

    with input_path.open("r", encoding="utf-8") as source, _atomic_jsonl_writer(state_path) as state_out, _atomic_jsonl_writer(passed_path) as passed_out:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                blank_lines += 1
                continue
            records_read += 1
            try:
                row = json.loads(line)
                if not isinstance(row, Mapping):
                    raise ValueError("JSONL record must be an object")
                state, passed = screen_capture_record(row, profile, line_number=line_number)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                malformed_lines += 1
                state = {
                    "schema_version": SCREENING_SCHEMA_VERSION,
                    "capture_id": f"offerbiu-{line_number:06d}",
                    "source": {"input_line": line_number},
                    "job": {},
                    "status": "deferred",
                    "rule_status": "input_json_invalid",
                    "reasons": [f"input_json_invalid:{type(exc).__name__}:{_text(exc)}"],
                    "direction_classification": None,
                    "screening_result": None,
                    "capture": None,
                }
                passed = None
            state_out.write(state)
            passed_out.write(passed) if passed is not None else None
            counts[state["status"]] += 1
            rule_counts[state["rule_status"]] += 1
            capture = state.get("capture")
            if isinstance(capture, Mapping):
                candidate_available += bool(capture.get("candidate_available"))
                candidate_used += bool(capture.get("candidate_used"))

    summary = {
        "schema_version": SCREENING_SCHEMA_VERSION,
        "mode": "screen",
        "input": {"path": str(input_path), "format": "JSONL", "job_field": "job"},
        "hydration_schema": {
            "original_job": "job",
            "candidate_jd": "candidate_jd_raw",
            "candidate_capture_evidence": "candidate_capture_evidence",
            "candidate_used_only_when_capture_assessment_is_complete": True,
        },
        "policy": {
            "screen": "packages.matching.rules.screen_job",
            "directions": "packages.matching.rules.classify_job_directions",
            "models_called": 0,
            "network_calls": 0,
            "database_writes": 0,
        },
        "profile_fingerprint": profile_fingerprint(profile),
        "records_read": records_read,
        "blank_lines": blank_lines,
        "malformed_lines": malformed_lines,
        "counts": {key: counts.get(key, 0) for key in ("passed", "excluded", "deferred")},
        "rule_status_counts": dict(sorted(rule_counts.items())),
        "candidate_available_records": candidate_available,
        "candidate_used_records": candidate_used,
        "files": {"all_state": state_path.name, "passed_only": passed_path.name},
        "conservative_boundary": [
            "Deferred evidence or unclear direction is never emitted to passed-only.",
            "The effective job uses candidate_jd_raw only after verified capture evidence.",
            "No model, network, or database operation is performed.",
        ],
    }
    _atomic_json_writer(summary_path, summary)
    return summary


def _url_from_mapping(value: Mapping[str, Any]) -> str:
    return _text(_first(value, _IDENTITY_URL_FIELDS))


def _canonical_url(value: Any) -> str:
    return normalize_job_identity_url(_text(value))


def _tenant_from_url(url: str, source_kind: Any = None) -> str:
    if not url:
        return ""
    try:
        return _casefold(source_identity_for_url(url, _text(source_kind)))
    except (TypeError, ValueError):
        return ""


def _source_platform(row: Mapping[str, Any], job: Mapping[str, Any]) -> str:
    return _casefold(
        _first(job, ("source_platform", "platform", "crawler_key", "crawler"))
        or _first(row, ("source_platform", "platform", "crawler_key", "crawler"))
    )


def _native_id_from_url(url: str, source_platform: str = "", title: str = "") -> str:
    """Parse only identities covered by a known official route.

    Generic ``/detail/<id>`` and arbitrary query parameters are intentionally
    not identities.  Explicit catalog/hydration native IDs remain usable when
    a platform has no parser here.
    """

    if not url:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""

    # This is the same controlled route parser used by verified detail reuse.
    # A synthetic title is enough to ask it for the route post ID.
    try:
        controlled = build_detail_reuse_key(
            {"detail_url": url, "title": title or "__identity__"}
        )
    except (TypeError, ValueError):
        controlled = None
    if controlled is not None:
        return _text(controlled.post_id)

    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path.rstrip("/").casefold()
    platform = _casefold(source_platform)

    # Moka's job ID is carried by a controlled hash route on the official
    # app.mokahr.com host.  The surrounding campaign route is required.
    moka_host = host == "app.mokahr.com"
    direct_moka_match = re.fullmatch(r"/job/([^/?#]+)", parsed.path, re.I)
    moka_campaign = bool(
        re.fullmatch(
            r"/(?:campus_apply|campus-recruitment|social-recruitment|campus|social)/[^/]+/[^/]+",
            path,
        )
    )
    if moka_host and (moka_campaign or direct_moka_match):
        match = re.fullmatch(r"/(?:job|jobs)/([^/?#]+)", parsed.fragment, re.I)
        if match and _text(match.group(1)):
            return _text(match.group(1))
        if direct_moka_match and _text(direct_moka_match.group(1)):
            return _text(direct_moka_match.group(1))

    # Beisen's query IDs are accepted only with a known Beisen platform or a
    # known campus jobs route.  Unknown hosts/platforms do not get a guessed ID.
    beisen_host = host.endswith(".zhiye.com") or host == "zhiye.com"
    beisen_route = "/campus/jobs" in path or "/campus/job" in path
    if (platform in {"beisen", "beisen_mobile"} or (beisen_host and beisen_route)):
        query = parse_qs(parsed.query)
        for key, values in query.items():
            if key.casefold() in {"jobid", "job_id", "jobadid", "job_ad_id", "positionid", "position_id", "postid", "post_id"}:
                if values and _text(values[0]):
                    return _text(values[0])
    return ""


def _identity_value(row: Mapping[str, Any], job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = row.get("identity")
    if isinstance(value, Mapping):
        return value
    value = job.get("identity")
    return value if isinstance(value, Mapping) else None


def _identity_url_values(row: Mapping[str, Any], job: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for value in (job, row):
        for field in _IDENTITY_URL_FIELDS:
            raw = _text(value.get(field))
            if not raw:
                continue
            normalized = _canonical_url(raw)
            if normalized and normalized not in values:
                values.append(normalized)
    return values


def _identity_native_values(row: Mapping[str, Any], job: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for value in (job, row):
        for field in _NATIVE_ID_FIELDS:
            raw = _text(value.get(field))
            if raw and _casefold(raw) not in {_casefold(item) for item in values}:
                values.append(raw)
    identity = _identity_value(row, job)
    if identity:
        identity_type = _casefold(identity.get("type"))
        identity_raw = _text(identity.get("value"))
        if "native" in identity_type and identity_raw:
            native_raw = identity_raw.rsplit(":", 1)[-1]
            if _casefold(native_raw) not in {_casefold(item) for item in values}:
                values.append(native_raw)
    return values


def _identity_fields(row: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
    url_values = _identity_url_values(row, job)
    url = url_values[0] if url_values else ""
    source_kind = _source_platform(row, job)
    tenant = _casefold(_first(job, _TENANT_FIELDS) or _first(row, _TENANT_FIELDS))
    if not tenant:
        tenant = _tenant_from_url(url, source_kind)

    explicit_native_values = _identity_native_values(row, job)
    legacy_synthetic = False
    legacy_synthetic_id = _text(job.get("native_job_id"))
    try:
        legacy_synthetic = bool(_is_reproducible_synthetic_id(job))
    except (TypeError, ValueError, KeyError):
        legacy_synthetic = False
    if legacy_synthetic and legacy_synthetic_id:
        explicit_native_values = [
            value
            for value in explicit_native_values
            if _casefold(value) != _casefold(legacy_synthetic_id)
        ]
    native_id = explicit_native_values[0] if explicit_native_values else ""
    parsed_native_id = _native_id_from_url(
        _url_from_mapping(job) or _url_from_mapping(row),
        source_kind,
        _text(job.get("title")),
    )
    if not native_id:
        native_id = parsed_native_id

    business_key = _casefold(_first(job, _BUSINESS_KEY_FIELDS) or _first(row, _BUSINESS_KEY_FIELDS))
    identity_conflicts: list[str] = []
    if len(url_values) > 1:
        identity_conflicts.append("detail_url_conflict")
    if len(explicit_native_values) > 1:
        identity_conflicts.append("explicit_native_id_conflict")
    if parsed_native_id and explicit_native_values and any(
        _casefold(parsed_native_id) != _casefold(value) for value in explicit_native_values
    ):
        identity_conflicts.append("url_native_id_conflict")
    keys: dict[str, Any] = {}
    if business_key:
        keys["business_key"] = business_key
    if url:
        keys["canonical_url"] = url
    if tenant and native_id:
        keys["tenant_native_id"] = f"{tenant}\x00{_casefold(native_id)}"
    return {
        "business_key": business_key or None,
        "canonical_url": url or None,
        "tenant": tenant or None,
        "native_job_id": native_id or None,
        "parsed_native_job_id": parsed_native_id or None,
        "legacy_synthetic": legacy_synthetic,
        "legacy_synthetic_native_job_id": legacy_synthetic_id if legacy_synthetic else None,
        "tenant_native_id": f"{tenant}\x00{_casefold(native_id)}" if tenant and native_id else None,
        "keys": keys,
        "identity_conflicts": sorted(set(identity_conflicts)),
    }


@dataclass(frozen=True)
class _Company:
    company_id: str
    name: str
    names: frozenset[str]


@dataclass(frozen=True)
class _ExistingJob:
    job_id: str
    row: Mapping[str, Any]
    company_id: str
    company_names: frozenset[str]
    identity: Mapping[str, Any]
    title_key: str


class _SnapshotIndex:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.companies: dict[str, _Company] = {}
        self.company_name_ids: dict[str, set[str]] = defaultdict(set)
        self.jobs: dict[str, _ExistingJob] = {}
        self.by_key: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.by_title: dict[str, list[str]] = defaultdict(list)
        self.analyses: dict[str, Mapping[str, Any]] = {}

        if "read_only" in payload and payload.get("read_only") is not True:
            raise ValueError("snapshot read_only must be true")
        if "application_audit" in payload and not isinstance(payload.get("application_audit"), Mapping):
            raise ValueError("snapshot application_audit must be an object")
        companies = payload.get("companies")
        jobs = payload.get("jobs")
        analyses = payload.get("analyses")
        if not all(isinstance(value, list) for value in (companies, jobs, analyses)):
            raise ValueError("snapshot companies, jobs, and analyses must be arrays")

        for item in companies:
            if not isinstance(item, Mapping):
                raise ValueError("snapshot company rows must be objects")
            company_id = _text(item.get("id"))
            name = _text(item.get("name"))
            if not company_id or not name:
                raise ValueError("snapshot companies require id and name")
            names = frozenset(
                normalize_company_name(candidate)
                for candidate in (name, *_string_list(item.get("aliases")))
                if normalize_company_name(candidate)
            )
            company = _Company(company_id, name, names)
            self.companies[company_id] = company
            for name_key in names:
                self.company_name_ids[name_key].add(company_id)

        for item in jobs:
            if not isinstance(item, Mapping):
                raise ValueError("snapshot job rows must be objects")
            job_id = _text(item.get("id"))
            if not job_id:
                raise ValueError("snapshot jobs require id")
            if job_id in self.jobs:
                raise ValueError(f"duplicate snapshot job id: {job_id}")
            company_id = _text(item.get("company_id"))
            company = self.companies.get(company_id)
            job_company_names = {
                normalize_company_name(candidate)
                for candidate in (
                    item.get("company"),
                    item.get("company_name"),
                    company.name if company else "",
                    *(company.names if company else ()),
                )
                if normalize_company_name(candidate)
            }
            identity = _identity_fields(item, item)
            existing = _ExistingJob(
                job_id=job_id,
                row=item,
                company_id=company_id,
                company_names=frozenset(job_company_names),
                identity=identity,
                title_key=normalize_job_title(item.get("title")),
            )
            self.jobs[job_id] = existing
            for method, value in identity["keys"].items():
                self.by_key[(method, value)].append(job_id)
            if existing.title_key:
                self.by_title[existing.title_key].append(job_id)

        for item in analyses:
            if not isinstance(item, Mapping):
                raise ValueError("snapshot analysis rows must be objects")
            job_id = _text(item.get("job_id"))
            if not job_id:
                raise ValueError("snapshot analyses require job_id")
            if job_id in self.analyses:
                raise ValueError(f"duplicate snapshot analysis job_id: {job_id}")
            self.analyses[job_id] = item


def _candidate_company_info(row: Mapping[str, Any], job: Mapping[str, Any], index: _SnapshotIndex) -> tuple[str, set[str], set[str]]:
    company_id = _text(job.get("company_id") or row.get("company_id"))
    name = _text(row.get("company") or job.get("company") or job.get("company_name"))
    name_key = normalize_company_name(name)
    mapped_ids = set(index.company_name_ids.get(name_key, set())) if name_key else set()
    return company_id, mapped_ids, {name_key} if name_key else set()


def _company_relation(
    row: Mapping[str, Any],
    job: Mapping[str, Any],
    existing: _ExistingJob,
    index: _SnapshotIndex,
) -> str:
    candidate_id, mapped_ids, candidate_names = _candidate_company_info(row, job, index)
    if candidate_id:
        if candidate_id == existing.company_id:
            return "same"
        if existing.company_id:
            return "different"
    if len(mapped_ids) > 1:
        return "ambiguous"
    if mapped_ids:
        return "same" if existing.company_id in mapped_ids else "different"
    if candidate_names and candidate_names.intersection(existing.company_names):
        return "same"
    if candidate_names:
        return "different"
    return "unknown"


def _identity_pending_result(
    capture: Mapping[str, Any],
    identity: Mapping[str, Any],
    reasons: list[str],
    *,
    title_conflicts: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": "identity_pending",
        "bucket": "identity_pending",
        "capture_id": _text(capture.get("capture_id")),
        "existing_job_id": None,
        "match_method": None,
        "reasons": list(dict.fromkeys(_text(reason) for reason in reasons if _text(reason))),
        "identity": dict(identity),
        "existing_identity": None,
        "title_conflicts": title_conflicts or [],
        "analysis": None,
    }


def _identity_pair_conflicts(
    current: Mapping[str, Any],
    existing: Mapping[str, Any],
) -> list[str]:
    """Return contradictions that a shared key must not hide."""

    reasons: list[str] = []
    current_url = _casefold(current.get("canonical_url"))
    existing_url = _casefold(existing.get("canonical_url"))
    current_business = _casefold(current.get("business_key"))
    existing_business = _casefold(existing.get("business_key"))
    current_native = _casefold(current.get("native_job_id"))
    existing_native = _casefold(existing.get("native_job_id"))
    current_tenant = _casefold(current.get("tenant"))
    existing_tenant = _casefold(existing.get("tenant"))

    if current_url and current_url == existing_url:
        if current_native and existing_native and current_native != existing_native:
            reasons.append("url_native_id_conflict")
        if current_tenant and existing_tenant and current_tenant != existing_tenant:
            reasons.append("url_tenant_conflict")
    if current_business and current_business == existing_business:
        if current_native and existing_native and current_native != existing_native:
            reasons.append("business_key_native_id_conflict")
        if current_tenant and existing_tenant and current_tenant != existing_tenant:
            reasons.append("business_key_tenant_conflict")
    return sorted(set(reasons))


def _analysis_payload(
    current_job: Mapping[str, Any],
    existing: _ExistingJob,
    analysis: Mapping[str, Any] | None,
) -> dict[str, Any]:
    current_jd = _text(current_job.get("jd_raw"))
    existing_jd = _text(existing.row.get("jd_raw"))
    current_fp = content_fingerprint(current_job)
    analysis_fp = _text(_first(analysis or {}, _ANALYSIS_FINGERPRINT_FIELDS))
    existing_fp = content_fingerprint(existing.row) if existing_jd else ""

    if not current_jd:
        content_changed = False
        change_reason = "current_jd_empty_preserve_existing"
    elif existing_jd:
        # The frozen hydration schema and the catalog schema do not carry the
        # same optional metadata (for example batch/recruitment_track).  JD
        # equality is therefore the stable change gate for a cross-schema
        # read-only comparison; analysis fingerprints remain evidence below.
        content_changed = _sha256_text(current_jd) != _sha256_text(existing_jd)
        change_reason = "jd_raw_changed" if content_changed else "content_unchanged"
    elif analysis_fp:
        content_changed = analysis_fp != current_fp
        change_reason = "analysis_content_fingerprint_changed" if content_changed else "content_unchanged"
    else:
        content_changed = True
        change_reason = "existing_jd_missing_new_jd_present"

    raw_score = (analysis or {}).get("match_score")
    score = raw_score if raw_score is not None else None
    numeric_score: float | int | None = None
    if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
        try:
            if math.isfinite(float(raw_score)):
                numeric_score = raw_score
            else:
                score = None
        except (OverflowError, TypeError, ValueError):
            numeric_score = None
    analysis_status = _casefold((analysis or {}).get("analysis_status"))
    analysis_complete = analysis_status == "complete"
    score_valid = numeric_score is not None and 0 <= numeric_score <= 100
    scored = analysis_complete and score_valid
    return {
        "analysis_status": _text((analysis or {}).get("analysis_status")) or None,
        "match_score": score,
        "analysis_complete": analysis_complete,
        "score_valid": score_valid,
        "score_preserved": score is not None,
        "scored": scored,
        "analysis_version": _text((analysis or {}).get("analysis_version")) or None,
        "prompt_version": _text((analysis or {}).get("prompt_version")) or None,
        "analysis_content_fingerprint": analysis_fp or None,
        "existing_content_fingerprint": existing_fp or None,
        "current_content_fingerprint": current_fp,
        "current_jd_sha256": _sha256_text(current_jd) if current_jd else None,
        "existing_jd_sha256": _sha256_text(existing_jd) if existing_jd else None,
        "content_changed": content_changed,
        "change_reason": change_reason,
        "reuse_existing_score": bool(scored and not content_changed),
        "model_version_is_not_a_change_gate": True,
    }


def _compact_item(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "capture_id",
            "input_line",
            "existing_job_id",
            "status",
            "bucket",
            "match_method",
            "reasons",
            "analysis",
            "existing_identity",
            "title_conflicts",
            "distinct_work_key",
            "batch_company_identity_conflict_group",
            "source",
        )
    }


def _compact_source(capture: Mapping[str, Any], input_line: int) -> dict[str, Any]:
    source = capture.get("source")
    source_mapping = source if isinstance(source, Mapping) else {}
    job = capture.get("job")
    job_mapping = job if isinstance(job, Mapping) else {}
    return {
        "capture_id": _text(capture.get("capture_id")),
        "input_line": input_line,
        "job_key": _text(source_mapping.get("job_key")),
        "company_id": _text(source_mapping.get("company_id") or job_mapping.get("company_id")),
        "company": _text(source_mapping.get("company") or job_mapping.get("company") or job_mapping.get("company_name")),
        "title": _text(job_mapping.get("title")),
        "detail_url": _text(job_mapping.get("detail_url") or job_mapping.get("jd_url")),
        "native_job_id": _text(_first(job_mapping, _NATIVE_ID_FIELDS)),
        "business_key": _text(_first(job_mapping, _BUSINESS_KEY_FIELDS)),
        "crawl_url": _text(source_mapping.get("crawl_url")),
        "source_checkpoint": _text(source_mapping.get("source_checkpoint")),
        "source_index": source_mapping.get("source_index"),
        "source_labels": deepcopy(source_mapping.get("source_labels")) if source_mapping.get("source_labels") else [],
        "duplicate_count": source_mapping.get("duplicate_count", 0),
        "duplicate_sources": deepcopy(source_mapping.get("duplicate_sources")) if source_mapping.get("duplicate_sources") else [],
    }


def _attach_reconciliation_context(
    result: dict[str, Any],
    capture: Mapping[str, Any] | None,
    input_line: int,
) -> dict[str, Any]:
    if capture is None:
        result["source"] = {"input_line": input_line}
        result["job_summary"] = {}
        return result
    result["source"] = _compact_source(capture, input_line)
    job = capture.get("job")
    result["job_summary"] = _job_summary(job) if isinstance(job, Mapping) else {}
    return result


def _reconcile_one(
    capture: Mapping[str, Any],
    index: _SnapshotIndex,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    job = capture.get("job")
    if not isinstance(job, Mapping):
        return _identity_pending_result(capture, {}, ["passed_record_job_missing"]), None
    row = capture
    identity = _identity_fields(row, job)
    if identity.get("identity_conflicts"):
        return _identity_pending_result(
            capture,
            identity,
            ["identity_conflict", *identity["identity_conflicts"]],
        ), None
    candidate_ids: set[str] = set()
    methods: dict[str, list[str]] = {}
    for method, value in identity["keys"].items():
        ids = list(index.by_key.get((method, value), []))
        if ids:
            methods[method] = ids
            candidate_ids.update(ids)

    cross_tenant_native_ids = [
        job_id
        for job_id in candidate_ids
        if identity.get("native_job_id")
        and identity.get("tenant")
        and _casefold(index.jobs[job_id].identity.get("native_job_id")) == _casefold(identity["native_job_id"])
        and _casefold(index.jobs[job_id].identity.get("tenant"))
        and _casefold(index.jobs[job_id].identity.get("tenant")) != _casefold(identity["tenant"])
    ]
    if cross_tenant_native_ids:
        return _identity_pending_result(
            capture,
            identity,
            ["native_id_cross_tenant_not_merged"],
        ), None

    title_key = normalize_job_title(job.get("title"))
    title_conflicts = [
        job_id
        for job_id in index.by_title.get(title_key, [])
        if _company_relation(row, job, index.jobs[job_id], index) == "same"
    ] if title_key else []

    if not candidate_ids:
        if not identity["keys"]:
            return _identity_pending_result(
                capture,
                identity,
                ["controlled_identity_missing", "title_only_matching_disabled"],
                title_conflicts=title_conflicts,
            ), None
        reasons = ["controlled_identity_not_found"]
        if title_conflicts:
            reasons.append("same_title_not_merged")
        return {
            "status": "new",
            "bucket": "new",
            "capture_id": _text(capture.get("capture_id")),
            "existing_job_id": None,
            "match_method": None,
            "reasons": reasons,
            "identity": dict(identity),
            "title_conflicts": title_conflicts,
            "analysis": None,
        }, None

    relations = {job_id: _company_relation(row, job, index.jobs[job_id], index) for job_id in candidate_ids}
    same_ids = [job_id for job_id, relation in relations.items() if relation == "same"]
    if len(candidate_ids) != 1 or len(same_ids) != 1:
        reasons = ["identity_collision_or_conflict"]
        if any(relation == "different" for relation in relations.values()):
            reasons.append("same_native_or_url_across_companies_not_merged")
        if any(relation == "ambiguous" for relation in relations.values()):
            reasons.append("company_alias_matches_multiple_companies")
        return _identity_pending_result(capture, identity, reasons, title_conflicts=title_conflicts), None

    existing_id = same_ids[0]
    matched_methods = [method for method, ids in methods.items() if existing_id in ids]
    existing = index.jobs[existing_id]
    pair_conflicts = _identity_pair_conflicts(identity, existing.identity)
    if pair_conflicts or existing.identity.get("identity_conflicts"):
        return _identity_pending_result(
            capture,
            identity,
            [
                "identity_conflict",
                *pair_conflicts,
                *list(existing.identity.get("identity_conflicts") or []),
            ],
            title_conflicts=title_conflicts,
        ), None
    analysis = _analysis_payload(job, existing, index.analyses.get(existing_id))
    if analysis["content_changed"]:
        bucket = "changed"
        status = "matched_changed"
        reasons = [analysis["change_reason"]]
    elif analysis["scored"]:
        bucket = "scored"
        status = "matched_scored"
        reasons = ["existing_score_reused"]
    else:
        bucket = "pending"
        status = "matched_pending"
        reasons = [
            "existing_analysis_incomplete_or_score_invalid"
            if index.analyses.get(existing_id)
            else "existing_job_not_scored"
        ]
    result = {
        "status": status,
        "bucket": bucket,
        "capture_id": _text(capture.get("capture_id")),
        "existing_job_id": existing_id,
        "match_method": "+".join(sorted(matched_methods)),
        "reasons": reasons,
        "identity": dict(identity),
        "existing_identity": dict(existing.identity),
        "title_conflicts": title_conflicts,
        "analysis": analysis,
    }
    return result, existing.row


def _batch_key(capture: Mapping[str, Any]) -> tuple[str, str] | None:
    job = capture.get("job")
    if not isinstance(job, Mapping):
        return None
    identity = _identity_fields(capture, job)
    if identity.get("identity_conflicts"):
        return None
    for method in ("business_key", "canonical_url", "tenant_native_id"):
        value = identity.get(method)
        if value:
            return method, _text(value)
    return None


def _result_company_key(result: Mapping[str, Any], index: _SnapshotIndex) -> str:
    source = result.get("source")
    summary = result.get("job_summary")
    source_mapping = source if isinstance(source, Mapping) else {}
    summary_mapping = summary if isinstance(summary, Mapping) else {}
    explicit_company_id = _text(source_mapping.get("company_id") or summary_mapping.get("company_id"))
    if explicit_company_id:
        return f"id:{explicit_company_id}"
    company_name = _text(source_mapping.get("company") or summary_mapping.get("company"))
    company_key = normalize_company_name(company_name)
    mapped_ids = index.company_name_ids.get(company_key, set()) if company_key else set()
    if len(mapped_ids) == 1:
        return f"id:{next(iter(mapped_ids))}"
    if len(mapped_ids) > 1:
        return f"ambiguous:{'|'.join(sorted(mapped_ids))}"
    return f"name:{company_key}" if company_key else "unknown"


def _identity_descriptor(
    result: Mapping[str, Any],
    index: _SnapshotIndex,
    *,
    include_identity_pending: bool,
) -> tuple[str, str, str] | None:
    if not include_identity_pending and result.get("bucket") == "identity_pending":
        return None
    identity = result.get("identity")
    if not isinstance(identity, Mapping) or identity.get("identity_conflicts"):
        return None
    for method in ("tenant_native_id", "canonical_url", "business_key"):
        value = _text(identity.get(method))
        if value:
            return method, value, _result_company_key(result, index)
    return None


def _work_key(method: str, value: str, company_key: str) -> str:
    payload = json.dumps(
        [method, value, company_key],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"work-{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _defer_batch_company_conflicts(
    records: list[dict[str, Any]],
    index: _SnapshotIndex,
) -> list[dict[str, Any]]:
    # Connect observations through BOTH identity keys. A preferred native key
    # must not hide a URL collision with a row that has no native ID.
    by_key: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    row_keys: dict[int, list[tuple[str, str]]] = {}
    for position, result in enumerate(records):
        identity = result.get("identity") or {}
        keys = [
            (method, _text(identity[method]))
            for method in ("tenant_native_id", "canonical_url")
            if _text(identity.get(method))
        ]
        row_keys[position] = keys
        for key in keys:
            by_key[key].append(position)

    visited: set[int] = set()
    conflict_groups: list[dict[str, Any]] = []
    for start, result in enumerate(records):
        if start in visited or result.get("bucket") != "new":
            continue
        members: list[int] = []
        component_keys: set[tuple[str, str]] = set()
        pending = [start]
        while pending:
            position = pending.pop()
            if position in visited:
                continue
            visited.add(position)
            members.append(position)
            for key in row_keys[position]:
                if key not in component_keys:
                    component_keys.add(key)
                    pending.extend(by_key[key])
        company_keys = {_result_company_key(records[position], index) for position in members}
        if len(company_keys) < 2:
            continue
        group_key = "batch-conflict-" + _sha256_text(
            json.dumps(sorted(component_keys), ensure_ascii=False, separators=(",", ":"))
        )[:24]
        reclassified = 0
        for position in members:
            member = records[position]
            # Untrusted source labels may defer new observations, but cannot
            # invalidate a unique company/identity match to the catalog.
            if member.get("bucket") != "new":
                continue
            member["status"] = member["bucket"] = "identity_pending"
            member["reasons"] = [*member["reasons"], "batch_company_identity_conflict"]
            member["batch_company_identity_conflict_group"] = group_key
            member.pop("distinct_work_key", None)
            reclassified += 1
        conflict_groups.append(
            {
                "conflict_group_key": group_key,
                "reason": "batch_company_identity_conflict",
                "identity_keys": [
                    {"method": method, "value": value}
                    for method, value in sorted(component_keys)
                ],
                "company_keys": sorted(company_keys),
                "source_count": len(members),
                "reclassified_new_source_rows": reclassified,
                "sources": [
                    {
                        **deepcopy(records[position].get("source") or {}),
                        "bucket": records[position]["bucket"],
                        "existing_job_id": records[position].get("existing_job_id"),
                    }
                    for position in sorted(members)
                ],
            }
        )
    return sorted(conflict_groups, key=lambda group: group["conflict_group_key"])


def _attach_identity_groups(
    records: list[dict[str, Any]],
    index: _SnapshotIndex,
) -> dict[str, Any]:
    all_observations: set[tuple[str, str, str]] = set()
    safe_observations: set[tuple[str, str, str]] = set()
    identity_companies: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    groups: defaultdict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)

    for result in records:
        descriptor = _identity_descriptor(result, index, include_identity_pending=True)
        if descriptor:
            method, value, company_key = descriptor
            observation = (method, value, company_key)
            all_observations.add(observation)
            identity_companies[(method, value)].add(company_key)
        safe_descriptor = _identity_descriptor(result, index, include_identity_pending=False)
        if safe_descriptor:
            method, value, company_key = safe_descriptor
            safe_observations.add((method, value, company_key))
            if result.get("bucket") in {"new", "pending"}:
                distinct_work_key = _work_key(method, value, company_key)
                result["distinct_work_key"] = distinct_work_key
                groups[(result["bucket"], method, value, company_key)].append(result)

    group_rows: list[dict[str, Any]] = []
    for (bucket, method, value, company_key), members in groups.items():
        group_rows.append(
            {
                "distinct_work_key": _work_key(method, value, company_key),
                "bucket": bucket,
                "identity_method": method,
                "identity_value": value,
                "company_key": company_key,
                "source_count": len(members),
                "sources": [
                    {
                        **deepcopy(result.get("source") or {}),
                        "existing_job_id": result.get("existing_job_id"),
                    }
                    for result in members
                ],
            }
        )
    group_rows.sort(key=lambda item: (item["bucket"], item["distinct_work_key"]))
    conflict_groups = sum(
        len(company_keys) > 1 for company_keys in identity_companies.values()
    )
    return {
        "groups": group_rows,
        "official_identity_observations": len(all_observations),
        "unique_official_identity_groups": len(safe_observations),
        "identity_conflict_groups": conflict_groups,
        "new_unique_groups": sum(item["bucket"] == "new" for item in group_rows),
        "pending_unique_groups": sum(item["bucket"] == "pending" for item in group_rows),
    }


def _distinct_job_counts(records: Iterable[Mapping[str, Any]], new_groups: int) -> dict[str, int]:
    ids: defaultdict[str, set[str]] = defaultdict(set)
    for record in records:
        job_id = _text(record.get("existing_job_id"))
        if not job_id or record.get("bucket") == "identity_pending":
            continue
        state = "scored" if (record.get("analysis") or {}).get("scored") else "unscored"
        ids["existing_total"].add(job_id)
        ids[f"existing_{state}"].add(job_id)
        bucket = record.get("bucket")
        if bucket == "changed":
            ids["changed_total"].add(job_id)
            ids[f"changed_{state}"].add(job_id)
        elif bucket in {"pending", "scored"}:
            ids[f"{bucket}_bucket"].add(job_id)
    return {
        "new_unique_groups": new_groups,
        **{
            name: len(ids[name])
            for name in (
                "existing_total", "existing_scored", "existing_unscored",
                "changed_total", "changed_scored", "changed_unscored",
                "pending_bucket", "scored_bucket",
            )
        },
    }


def _changed_matrix(records: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, dict[str, int]]]:
    matrix = {
        complete: {
            changed: {score_state: 0 for score_state in ("scored", "unscored")}
            for changed in ("content_changed", "content_unchanged")
        }
        for complete in ("complete", "not_complete")
    }
    for record in records:
        if record.get("bucket") != "changed":
            continue
        analysis = record.get("analysis")
        analysis_mapping = analysis if isinstance(analysis, Mapping) else {}
        complete = "complete" if analysis_mapping.get("analysis_complete") else "not_complete"
        changed = "content_changed" if analysis_mapping.get("content_changed") else "content_unchanged"
        score_state = "scored" if analysis_mapping.get("scored") else "unscored"
        matrix[complete][changed][score_state] += 1
    return matrix


def _write_reconciliation_detail(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    with _atomic_jsonl_writer(path) as output:
        for record in records:
            output.write(record)


def reconcile_passed_jsonl(
    passed_path: str | Path,
    snapshot_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Reconcile passed-only rows against a supplied JSON snapshot only."""

    passed_path = Path(passed_path)
    snapshot_path = Path(snapshot_path)
    output_dir = Path(output_dir)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot root must be an object")
    index = _SnapshotIndex(snapshot)

    detail_path = output_dir / "reconciliation.jsonl"
    report_path = output_dir / "reconciliation-report.json"
    groups_path = output_dir / "groups.jsonl"
    conflicts_path = output_dir / "batch-conflicts.jsonl"
    records: list[dict[str, Any]] = []
    batch_keys: Counter[tuple[str, str]] = Counter()
    input_lines = 0
    malformed = 0

    with passed_path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            input_lines += 1
            try:
                capture = json.loads(line)
                if not isinstance(capture, Mapping):
                    raise ValueError("passed-only record must be an object")
                status = _casefold(capture.get("status"))
                if status and status != "passed":
                    raise ValueError("passed-only record has non-passed status")
                result, _ = _reconcile_one(capture, index)
                result["input_line"] = line_number
                _attach_reconciliation_context(result, capture, line_number)
                key = _batch_key(capture)
                if key:
                    batch_keys[key] += 1
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                malformed += 1
                result = {
                    "status": "identity_pending",
                    "bucket": "identity_pending",
                    "capture_id": f"unknown-line-{line_number:06d}",
                    "existing_job_id": None,
                    "match_method": None,
                    "reasons": [f"passed_input_invalid:{type(exc).__name__}:{_text(exc)}"],
                    "identity": {},
                    "existing_identity": None,
                    "title_conflicts": [],
                    "analysis": None,
                    "input_line": line_number,
                }
                _attach_reconciliation_context(result, None, line_number)
            records.append(result)

    duplicate_records = sum(max(count - 1, 0) for count in batch_keys.values())
    category_names = ("new", "scored", "pending", "changed", "identity_pending")
    batch_conflicts = _defer_batch_company_conflicts(records, index)
    _write_reconciliation_detail(conflicts_path, batch_conflicts)
    group_stats = _attach_identity_groups(records, index)
    distinct_counts = _distinct_job_counts(records, group_stats["new_unique_groups"])
    with _atomic_jsonl_writer(groups_path) as groups_out:
        for group in group_stats["groups"]:
            groups_out.write(group)
    _write_reconciliation_detail(detail_path, records)
    categories = {
        name: [_compact_item(record) for record in records if record.get("bucket") == name]
        for name in category_names
    }
    counts = {name: len(categories[name]) for name in category_names}
    changed_matrix = _changed_matrix(records)
    changed_scored = sum(
        analysis.get("scored", False)
        for record in records
        if record.get("bucket") == "changed"
        for analysis in [record.get("analysis") or {}]
        if isinstance(analysis, Mapping)
    )
    existing_job_ids = {
        _text(record.get("existing_job_id"))
        for record in records
        if _text(record.get("existing_job_id"))
    }
    snapshot_legacy_synthetic_jobs = sum(
        bool(existing.identity.get("legacy_synthetic"))
        for existing in index.jobs.values()
    )
    matched_legacy_synthetic_rows = sum(
        bool((record.get("existing_identity") or {}).get("legacy_synthetic"))
        for record in records
    )
    report = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "mode": "reconcile",
        "inputs": {
            "passed_only": str(passed_path),
            "snapshot": str(snapshot_path),
            "snapshot_read_only": True,
        },
        "snapshot_contract": deepcopy(SNAPSHOT_CONTRACT),
        "snapshot_counts": {
            "companies": len(index.companies),
            "jobs": len(index.jobs),
            "analyses": len(index.analyses),
            "read_only": snapshot.get("read_only", True),
        },
        "legacy_synthetic_normalization": {
            "snapshot_legacy_synthetic_jobs": snapshot_legacy_synthetic_jobs,
            "matched_source_rows": matched_legacy_synthetic_rows,
            "diagnostic_field": "reconciliation.jsonl[].existing_identity.legacy_synthetic",
            "snapshot_unchanged": True,
            "known_route_id_preferred": True,
            "unknown_route_fallback": "canonical_url_plus_unique_company",
            "arbitrary_non_reproducible_hex_remains_conflict": True,
        },
        "records_read": input_lines,
        "malformed_records": malformed,
        "batch_company_identity_conflicts": {
            "identity_components": len(batch_conflicts),
            "reclassified_new_source_rows": sum(
                group["reclassified_new_source_rows"] for group in batch_conflicts
            ),
            "validated_catalog_matches_preserved": True,
        },
        "distinct_job_counts": distinct_counts,
        "potential_scoring_work": {
            "new_unique_groups": distinct_counts["new_unique_groups"],
            "existing_unscored_unique_jobs": distinct_counts["existing_unscored"],
            "total": distinct_counts["new_unique_groups"] + distinct_counts["existing_unscored"],
            "identity_pending_excluded": True,
            "existing_deduplication_key": "existing_job_id",
            "changed_scored_default_rescore": 0,
        },
        "counts": {
            **counts,
            "batch_duplicate_records": duplicate_records,
            "batch_duplicate_identity_groups": sum(count > 1 for count in batch_keys.values()),
        },
        "source_row_counts": {
            "passed_source_rows": input_lines,
            "unique_official_identity_groups": group_stats["unique_official_identity_groups"],
            "official_identity_observations": group_stats["official_identity_observations"],
            "unique_new_groups": group_stats["new_unique_groups"],
            "unique_pending_groups": group_stats["pending_unique_groups"],
            "unique_existing_job_ids": len(existing_job_ids),
            "identity_conflict_groups": group_stats["identity_conflict_groups"],
            "identity_pending_source_rows": counts["identity_pending"],
            "matched_legacy_synthetic_rows": matched_legacy_synthetic_rows,
            "batch_duplicate_records": duplicate_records,
            "batch_duplicate_identity_groups": sum(count > 1 for count in batch_keys.values()),
        },
        "categories": categories,
        "groups": {
            "allowed_buckets": ["new", "pending"],
            "identity_scope": "official tenant/native ID, canonical URL, or explicit business key plus company scope",
            "cross_company_identity_is_not_merged": True,
            "count": len(group_stats["groups"]),
        },
        "files": {
            "details": detail_path.name,
            "groups": groups_path.name,
            "batch_identity_conflicts": conflicts_path.name,
        },
        "id_mapping": {
            "capture_id": "passed-only.capture_id",
            "existing_job_id": "snapshot.jobs[].id when a controlled identity match is proven",
            "unmatched_existing_job_id": None,
        },
        "analysis_policy": {
            "scored_reuse_ignores_analysis_or_model_version": True,
            "content_change_is_reported_separately": True,
            "unscored_existing_jobs_are_pending": True,
            "score_reuse_requires_analysis_status_complete": True,
            "score_reuse_requires_finite_score_0_to_100": True,
            "failed_analysis_with_old_score_is_pending": True,
            "changed_scores_are_preserved_without_rescoring": True,
            "default_rescore_count": 0,
            "this_run_scores_nothing": True,
            "formal_database_writes": 0,
        },
        "changed_report": {
            "total": counts["changed"],
            "scored": changed_scored,
            "unscored": counts["changed"] - changed_scored,
            "score_preserved": sum(
                bool((record.get("analysis") or {}).get("score_preserved"))
                for record in records
                if record.get("bucket") == "changed"
            ),
            "default_rescore": 0,
            "matrix": changed_matrix,
        },
        "changed_matrix": changed_matrix,
        "conservative_boundary": [
            "A native ID is scoped by its official tenant; the same ID across tenants is not merged.",
            "A shared portal collision across companies remains identity_pending unless one safe identity is unique.",
            "Batch-only company conflicts on any native/URL key defer new rows and preserve validated catalog matches.",
            "A normalized URL or explicit company alias may confirm identity; title similarity never confirms it.",
            "An empty current JD never overwrites or marks the existing JD changed.",
            "No formal database or network access is performed.",
        ],
    }
    _atomic_json_writer(report_path, report)
    return report


__all__ = [
    "RECONCILIATION_SCHEMA_VERSION",
    "SCREENING_SCHEMA_VERSION",
    "SNAPSHOT_CONTRACT",
    "reconcile_passed_jsonl",
    "screen_capture_jsonl",
    "screen_capture_record",
]
