"""Safely import offline Luna review results into existing snapshots.

The importer is deliberately independent of the online matching client.  Luna
has already produced the review JSON; this module only validates its provenance
and evidence against a frozen catalog and the current Agent database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from packages.matching.models import (
    AnalysisStatus,
    DeepSeekMatchPayload,
    EvidenceRelation,
)
from packages.matching.rules import content_fingerprint, profile_fingerprint, screen_job
from packages.recruitment_core.jd_capture import assess_jd_capture
from packages.matching.service import (
    ANALYSIS_VERSION,
    PROMPT_VERSION,
    _score,
)
from packages.storage import CompanySnapshot, JobAnalysisSnapshot, JobSnapshot, Storage


EXPECTED_MODEL = "gpt-5.6-luna"
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_JOB_FIELDS = (
    "id",
    "company",
    "company_id",
    "title",
    "city",
    "detail_url",
    "jd_raw",
    "cohort",
    "cohort_status",
    "batch",
    "source",
    "source_ref",
    "content_fingerprint",
)
_IDENTITY_FIELDS = (
    "company_id",
    "title",
    "city",
    "detail_url",
    "cohort",
    "cohort_status",
    "batch",
    "source",
    "source_ref",
)
_EXCLUDE_REASON_GENERIC_FRAGMENTS = {
    "岗位",
    "职位",
    "方向",
    "职责",
    "要求",
    "岗位职责",
    "任职要求",
    "岗位要求",
    "职位描述",
    "岗位描述",
    "工作职责",
    "目标方向",
}


class ReviewImportError(ValueError):
    """Raised when the manifest or an apply operation cannot be trusted."""


class ReviewImportTransactionError(ReviewImportError):
    """Raised after an apply transaction has been rolled back."""


@dataclass(frozen=True)
class Rejection:
    job_id: str | None
    reason: str
    source: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        result: dict[str, str | None] = {
            "job_id": self.job_id,
            "reason": self.reason,
        }
        if self.source is not None:
            result["source"] = self.source
        return result


@dataclass
class ReviewImportReport:
    planned: int = 0
    written: int = 0
    reused: int = 0
    rejected: list[Rejection] = field(default_factory=list)
    dry_run: bool = True

    def as_dict(self) -> dict[str, Any]:
        reasons: dict[str, int] = {}
        for rejection in self.rejected:
            reasons[rejection.reason] = reasons.get(rejection.reason, 0) + 1
        return {
            "planned": self.planned,
            "written": self.written,
            "reused": self.reused,
            "rejected": [item.as_dict() for item in self.rejected],
            "rejected_count": len(self.rejected),
            "rejected_reasons": dict(sorted(reasons.items())),
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class _Manifest:
    run_id: str
    review_mode: str
    review_model: str
    profile: Mapping[str, Any]
    profile_fingerprint: str
    jobs: Mapping[str, Mapping[str, Any]]
    backup: Mapping[str, Any] | None


@dataclass(frozen=True)
class _RawReview:
    job_id: str
    review: Mapping[str, Any]
    source: str


@dataclass(frozen=True)
class _PlannedWrite:
    job_id: str
    frozen_job: Mapping[str, Any]
    decision: str
    reason: str
    analysis: DeepSeekMatchPayload | None
    score: int | None
    recommendation: str | None
    current_fingerprint: str
    profile_fingerprint: str
    persisted_status: str
    review_mode: str = "legacy"
    review_model: str = EXPECTED_MODEL
    replace_complete: bool = False
    previous_analysis: Mapping[str, Any] | None = None


def load_json_file(path: Path | str) -> Any:
    """Load one UTF-8 JSON artifact without modifying it."""

    resolved = Path(path).expanduser()
    try:
        return json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewImportError(f"unable to read JSON: {resolved}") from exc


def _text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _compact(value: Any) -> str:
    return " ".join(_text(value).split())


def _digest(value: Path) -> str:
    hasher = sha256()
    with value.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _require_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ReviewImportError(f"{field_name}_invalid")
    return value


def _normalised_variants(value: Any) -> tuple[str, str]:
    """Return whitespace-preserving and punctuation-insensitive search forms."""

    raw = unicodedata.normalize("NFKC", _text(value)).casefold()
    spaced: list[str] = []
    compact: list[str] = []
    for character in raw:
        category = unicodedata.category(character)
        if category.startswith("C"):
            continue
        if category.startswith("P"):
            spaced.append(" ")
            continue
        spaced.append(character)
        if not character.isspace():
            compact.append(character)
    return " ".join("".join(spaced).split()), "".join(compact)


def _contains(source: Any, needle: Any) -> bool:
    source_spaced, source_compact = _normalised_variants(source)
    needle_spaced, needle_compact = _normalised_variants(needle)
    if not needle_spaced or not needle_compact:
        return False
    return needle_spaced in source_spaced or needle_compact in source_compact


def _exclude_reason_fragments(reason: str) -> tuple[str, ...]:
    """Extract concrete Chinese or technical fragments from an exclude reason."""

    normalized = unicodedata.normalize("NFKC", _text(reason)).casefold()
    fragments: set[str] = set(
        token
        for token in re.findall(r"[a-z0-9][a-z0-9+#.+-]{1,}", normalized)
        if token not in {"not", "and", "or", "the", "job", "role", "target"}
    )
    for run in re.findall(r"[\u3400-\u9fff]+", normalized):
        for length in range(2, min(8, len(run)) + 1):
            fragments.update(run[index : index + length] for index in range(len(run) - length + 1))
    return tuple(
        fragment
        for fragment in fragments
        if fragment and fragment not in _EXCLUDE_REASON_GENERIC_FRAGMENTS
    )


def _validate_exclude_reason(reason: str, job: Mapping[str, Any]) -> str | None:
    """Require an exclude reason to cite the frozen title or JD text."""

    if not _compact(reason):
        return "reason_required"
    fragments = _exclude_reason_fragments(reason)
    sources = (job.get("title"), job.get("jd_raw"))
    if any(_contains(source, fragment) for source in sources for fragment in fragments):
        return None
    return "exclude_reason_evidence_missing"


def _profile_leaves(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        leaves: list[str] = []
        for child in value.values():
            leaves.extend(_profile_leaves(child))
        return leaves
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        leaves = []
        for child in value:
            leaves.extend(_profile_leaves(child))
        return leaves
    value_text = _compact(value)
    return [value_text] if value_text else []


def _verified_profile_matches(
    profile: Mapping[str, Any],
    evidence: str,
) -> set[str]:
    """Return only verified profile sections that contain the evidence text."""

    matches: set[str] = set()
    if any(_contains(leaf, evidence) for leaf in _profile_leaves(profile.get("skills", []))):
        matches.add("skills")
    matching = profile.get("matching")
    if isinstance(matching, Mapping):
        for section in ("project_evidence", "supporting_skills"):
            if any(_contains(leaf, evidence) for leaf in _profile_leaves(matching.get(section, []))):
                matches.add(section)
    return matches


def _plain_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="python")
        return dumped if isinstance(dumped, Mapping) else None
    return None


def _validate_manifest(value: Any) -> _Manifest:
    manifest = _plain_mapping(value)
    if manifest is None:
        raise ReviewImportError("manifest_not_object")

    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ReviewImportError("manifest_run_id_invalid")
    profile = manifest.get("profile")
    if not isinstance(profile, Mapping):
        raise ReviewImportError("manifest_profile_invalid")
    expected_profile_fingerprint = _require_digest(
        manifest.get("profile_fingerprint"), "profile_fingerprint"
    )
    if profile_fingerprint(profile) != expected_profile_fingerprint:
        raise ReviewImportError("profile_fingerprint_mismatch")

    raw_jobs = manifest.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ReviewImportError("manifest_jobs_invalid")
    jobs: dict[str, Mapping[str, Any]] = {}
    for raw_job in raw_jobs:
        job = _plain_mapping(raw_job)
        if job is None:
            raise ReviewImportError("manifest_job_not_object")
        for field_name in _MANIFEST_JOB_FIELDS:
            if field_name not in job:
                raise ReviewImportError(f"manifest_job_missing:{field_name}")
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ReviewImportError("manifest_job_id_invalid")
        if job_id in jobs:
            raise ReviewImportError(f"manifest_duplicate_job_id:{job_id}")
        for field_name in (
            "company",
            "company_id",
            "title",
            "detail_url",
            "cohort_status",
            "batch",
            "source",
            "source_ref",
        ):
            if not isinstance(job.get(field_name), str) or not _compact(job[field_name]):
                raise ReviewImportError(f"manifest_job_field_invalid:{field_name}")
        if job.get("city") is not None and not isinstance(job.get("city"), str):
            raise ReviewImportError("manifest_job_field_invalid:city")
        if job.get("jd_raw") is not None and not isinstance(job.get("jd_raw"), str):
            raise ReviewImportError("manifest_job_field_invalid:jd_raw")
        content_digest = _require_digest(job.get("content_fingerprint"), "content_fingerprint")
        if content_fingerprint(job) != content_digest:
            raise ReviewImportError(f"content_fingerprint_mismatch:{job_id}")
        jobs[job_id] = job

    backup = manifest.get("backup")
    if backup is not None and not isinstance(backup, Mapping):
        raise ReviewImportError("manifest_backup_invalid")
    review_mode = str(manifest.get("review_mode") or "legacy")
    if review_mode not in {"legacy", "score_only"}:
        raise ReviewImportError("manifest_review_mode_invalid")
    review_model = str(manifest.get("review_model") or EXPECTED_MODEL).strip()
    if not review_model:
        raise ReviewImportError("manifest_review_model_invalid")
    return _Manifest(
        run_id=run_id.strip(),
        review_mode=review_mode,
        review_model=review_model,
        profile=profile,
        profile_fingerprint=expected_profile_fingerprint,
        jobs=jobs,
        backup=backup,
    )


def verify_manifest_backup(
    manifest: Mapping[str, Any] | _Manifest,
    *,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Verify the manifest's non-empty backup file and its recorded SHA-256."""

    frozen = manifest if isinstance(manifest, _Manifest) else _validate_manifest(manifest)
    backup = frozen.backup
    if backup is None:
        raise ReviewImportError("apply_backup_required")
    path_value = backup.get("path")
    expected = backup.get("sha256")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ReviewImportError("apply_backup_path_invalid")
    if not isinstance(expected, str) or _FINGERPRINT_RE.fullmatch(expected) is None:
        raise ReviewImportError("apply_backup_sha256_invalid")
    path = Path(path_value).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    path = path.resolve()
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            raise ReviewImportError("apply_backup_missing_or_empty")
        actual = _digest(path)
    except OSError as exc:
        raise ReviewImportError("apply_backup_unreadable") from exc
    if actual != expected:
        raise ReviewImportError("apply_backup_sha256_mismatch")
    return {"path": str(path), "sha256": actual, "bytes": path.stat().st_size}


def _result_documents(
    values: Mapping[str, Any] | Sequence[Any],
) -> list[tuple[str, Mapping[str, Any] | Any]]:
    if isinstance(values, Mapping) or isinstance(values, BaseModel):
        return [("result[0]", values)]
    documents: list[tuple[str, Mapping[str, Any] | Any]] = []
    for index, value in enumerate(values):
        if isinstance(value, tuple) and len(value) == 2:
            source, document = value
            documents.append((str(source), document))
        elif isinstance(value, (str, Path)):
            path = Path(value).expanduser()
            documents.append((str(path), load_json_file(path)))
        else:
            documents.append((f"result[{index}]", value))
    return documents


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _add_rejection(report: ReviewImportReport, job_id: str | None, reason: str, source: str) -> None:
    report.rejected.append(Rejection(job_id=job_id, reason=reason, source=source))


def _collect_reviews(
    frozen: _Manifest,
    documents: list[tuple[str, Mapping[str, Any] | Any]],
    report: ReviewImportReport,
) -> list[_RawReview]:
    occurrences: dict[str, list[_RawReview]] = {}
    for source, raw_document in documents:
        document = _plain_mapping(raw_document)
        if document is None:
            _add_rejection(report, None, "result_not_object", source)
            continue
        reviews = document.get("reviews")
        if not isinstance(reviews, list):
            _add_rejection(report, None, "reviews_not_list", source)
            continue
        run_reason = None
        if document.get("run_id") != frozen.run_id:
            run_reason = "run_id_mismatch"
        elif document.get("model") != frozen.review_model:
            run_reason = "model_mismatch"
        for raw_review in reviews:
            report.planned += 1
            review = _plain_mapping(raw_review)
            if review is None:
                _add_rejection(report, None, "review_not_object", source)
                continue
            job_id = review.get("job_id")
            if not isinstance(job_id, str) or not job_id.strip():
                _add_rejection(report, None, "review_job_id_invalid", source)
                continue
            job_id = job_id.strip()
            if run_reason is not None:
                _add_rejection(report, job_id, run_reason, source)
            elif job_id not in frozen.jobs:
                _add_rejection(report, job_id, "unknown_job_id", source)
            else:
                occurrences.setdefault(job_id, []).append(
                    _RawReview(job_id=job_id, review=review, source=source)
                )

    collected: list[_RawReview] = []
    for job_id, items in occurrences.items():
        if len(items) > 1:
            reason = "conflicting_duplicate_review"
            first = _canonical_json(items[0].review)
            if all(_canonical_json(item.review) == first for item in items[1:]):
                reason = "duplicate_job_id"
            for item in items:
                _add_rejection(report, item.job_id, reason, item.source)
            continue
        collected.append(items[0])
    return collected


def _review_shape(
    review: Mapping[str, Any],
    *,
    review_mode: str = "legacy",
) -> tuple[str, str, Mapping[str, Any] | None] | str:
    allowed = {"job_id", "decision", "reason", "analysis"}
    extra = set(review) - allowed
    if extra:
        return "review_extra_fields"
    decision = review.get("decision")
    if decision not in {"score", "defer", "exclude"}:
        return "decision_invalid"
    if review_mode == "score_only" and decision != "score":
        return "decision_not_allowed_in_score_only"
    reason = review.get("reason")
    if not isinstance(reason, str):
        return "reason_invalid"
    reason = reason.strip()
    if decision != "score" and not _compact(reason):
        return "reason_required"
    raw_analysis = review.get("analysis")
    if decision == "score":
        analysis = _plain_mapping(raw_analysis)
        if analysis is None:
            return "score_analysis_required"
        return decision, reason, analysis
    if raw_analysis is not None:
        return "analysis_not_allowed"
    return decision, reason, None


def _validate_payload(raw: Mapping[str, Any]) -> DeepSeekMatchPayload | str:
    required_fields = {
        "matched_directions",
        "primary_match_direction",
        "score_breakdown",
        "evidence_level",
        "evidence",
        "summary",
    }
    if not required_fields.issubset(raw):
        return "analysis_required_fields_missing"
    raw_directions = raw.get("matched_directions")
    if not isinstance(raw_directions, list) or not raw_directions:
        return "matched_directions_required"
    if raw.get("primary_match_direction") is None:
        return "primary_match_direction_required"
    try:
        raw_breakdown = raw.get("score_breakdown")
        if not isinstance(raw_breakdown, Mapping):
            return "score_breakdown_invalid"
        for field_name in (
            "core_direction",
            "required_skills",
            "project_evidence",
            "engineering_stack",
        ):
            if field_name not in raw_breakdown:
                return f"score_breakdown_missing:{field_name}"
            value = raw_breakdown[field_name]
            if isinstance(value, bool) or not isinstance(value, int):
                return "score_breakdown_non_integer"
        if not isinstance(raw.get("evidence"), list):
            return "evidence_list_invalid"
        if not isinstance(raw.get("summary"), str) or not _compact(raw.get("summary")):
            return "summary_required"
        payload = DeepSeekMatchPayload.model_validate(raw)
        if payload.primary_match_direction not in payload.matched_directions:
            return "primary_match_direction_mismatch"
        return payload
    except (ValidationError, TypeError, ValueError):
        return "analysis_schema_invalid"


def _validate_evidence(
    payload: DeepSeekMatchPayload,
    *,
    jd_raw: str | None,
    profile: Mapping[str, Any],
) -> str | None:
    if not _compact(jd_raw):
        return "evidence_invalid:jd_empty"
    if len(payload.evidence) < 2:
        return "evidence_invalid:at_least_two_required"
    seen: set[tuple[str, str, str]] = set()
    requirements: set[str] = set()
    headings = {"岗位职责", "职位描述", "工作职责", "任职要求", "岗位要求", "任职资格",
                "responsibilities", "requirements", "qualifications", "job description"}
    for index, evidence in enumerate(payload.evidence):
        requirement = _compact(evidence.jd_requirement)
        if not requirement:
            return f"evidence_invalid:{index}:jd_requirement_empty"
        if requirement.casefold().strip(':： ') in headings:
            return f"evidence_invalid:{index}:heading_not_requirement"
        requirements.add(requirement.casefold())
        if not _contains(jd_raw or "", requirement):
            return f"evidence_invalid:{index}:jd_requirement_not_in_jd"
        profile_evidence = _compact(evidence.profile_evidence)
        relation = evidence.relation.value
        evidence_key = (requirement, profile_evidence, relation)
        if evidence_key in seen:
            return f"evidence_invalid:{index}:duplicate"
        seen.add(evidence_key)
        if evidence.relation in {EvidenceRelation.DIRECT, EvidenceRelation.ADJACENT}:
            if not profile_evidence:
                return f"evidence_invalid:{index}:profile_evidence_required"
            matches = _verified_profile_matches(profile, profile_evidence)
            if not matches:
                return f"evidence_invalid:{index}:profile_evidence_not_in_profile"
            if (
                evidence.requirement_type.value == "core"
                and matches == {"supporting_skills"}
            ):
                return f"evidence_invalid:{index}:supporting_not_core_project"
        elif profile_evidence:
            matches = _verified_profile_matches(profile, profile_evidence)
            if not matches:
                return f"evidence_invalid:{index}:profile_evidence_not_in_profile"
    if len(requirements) < 2:
        return "evidence_invalid:two_distinct_requirements_required"
    return None


def _optional_identity(value: Any) -> str | None:
    if value is None:
        return None
    normalized = _compact(value)
    return normalized or None


def _identity_value(field_name: str, value: Any) -> Any:
    if field_name == "city":
        return _optional_identity(value)
    if field_name == "cohort":
        return value
    return _compact(value)


def _current_job_payload(
    row: JobSnapshot,
    company: CompanySnapshot,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "company": company.name,
        "company_id": row.company_id,
        "title": row.title,
        "city": row.city,
        "detail_url": row.detail_url,
        "jd_raw": row.jd_raw,
        "capture_evidence": row.capture_evidence or {},
        "cohort": row.cohort,
        "cohort_status": row.cohort_status,
        "batch": row.batch,
        "source": row.source,
        "source_ref": row.source_ref,
    }


def _state_reason(
    row: JobSnapshot,
    company: CompanySnapshot | None,
    frozen_job: Mapping[str, Any],
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    if company is None:
        return "company_not_found", None, None
    if row.id != frozen_job.get("id"):
        return "job_identity_mismatch:id", None, None
    for field_name in ("company", *_IDENTITY_FIELDS):
        expected = frozen_job.get(field_name)
        actual = company.name if field_name == "company" else getattr(row, field_name)
        if _identity_value(field_name, actual) != _identity_value(field_name, expected):
            return f"job_identity_mismatch:{field_name}", None, None
    payload = _current_job_payload(row, company)
    current_fingerprint = content_fingerprint(payload)
    if current_fingerprint != frozen_job.get("content_fingerprint"):
        return "stale_jd_fingerprint", payload, current_fingerprint
    return None, payload, current_fingerprint


def _same_fingerprint_complete(
    existing: JobAnalysisSnapshot | None,
    *,
    content_digest: str,
    profile_digest: str,
) -> bool:
    return bool(
        existing is not None
        and existing.analysis_status == "complete"
        and existing.content_fingerprint == content_digest
        and existing.profile_fingerprint == profile_digest
    )


def _same_non_score_decision(
    existing: JobAnalysisSnapshot | None,
    *,
    persisted_status: str,
    content_digest: str,
    profile_digest: str,
) -> bool:
    return bool(
        existing is not None
        and existing.analysis_status == persisted_status
        and existing.content_fingerprint == content_digest
        and existing.profile_fingerprint == profile_digest
        and existing.analysis_version == ANALYSIS_VERSION
        and existing.prompt_version == PROMPT_VERSION
        and existing.model == EXPECTED_MODEL
    )


def _non_score_status(decision: str, screening: Any) -> str | None:
    if decision == "defer":
        return AnalysisStatus.JD_INCOMPLETE.value
    if decision != "exclude":
        return None
    if screening.eligible:
        return AnalysisStatus.DIRECTION_OUT.value
    return screening.analysis_status.value


def _timestamp_key(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _previous_analysis_matches(
    existing: JobAnalysisSnapshot | None,
    previous: Mapping[str, Any] | None,
) -> bool:
    """Bind a replacement to the exact analysis captured by the manifest."""

    if existing is None or previous is None:
        return False
    required = (
        "content_fingerprint",
        "profile_fingerprint",
        "model",
        "match_score",
        "updated_at",
    )
    if any(field_name not in previous for field_name in required):
        return False
    return (
        existing.content_fingerprint == previous.get("content_fingerprint")
        and existing.profile_fingerprint == previous.get("profile_fingerprint")
        and existing.model == previous.get("model")
        and existing.match_score == previous.get("match_score")
        and _timestamp_key(existing.updated_at) == _timestamp_key(previous.get("updated_at"))
    )


def _json_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _score_values(
    payload: DeepSeekMatchPayload,
    score: int,
    recommendation: str,
    model: str = EXPECTED_MODEL,
) -> dict[str, Any]:
    return {
        "match_score": score,
        "advantages": json.dumps(payload.advantages, ensure_ascii=False),
        "gaps": json.dumps(
            [
                *payload.gaps,
                *[
                    item
                    for item in payload.missing_core_requirements
                    if item not in payload.gaps
                ],
            ],
            ensure_ascii=False,
        ),
        "summary": payload.summary,
        "recommendation": recommendation,
        "score_breakdown": payload.score_breakdown.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in payload.evidence],
        "evidence_level": payload.evidence_level.value,
        "matched_directions": [item.value for item in payload.matched_directions],
        "primary_match_direction": (
            payload.primary_match_direction.value
            if payload.primary_match_direction is not None
            else None
        ),
        "analysis_status": "complete",
        "model": model,
        "filter_reasons": [],
        "refusal_reason": None,
        "error_code": None,
    }


def _same_luna_score(
    existing: JobAnalysisSnapshot | None,
    *,
    content_digest: str,
    profile_digest: str,
    values: Mapping[str, Any],
    model: str = EXPECTED_MODEL,
) -> bool:
    if existing is None:
        return False
    if not (
        existing.analysis_status == "complete"
        and existing.model == model
        and existing.analysis_version == ANALYSIS_VERSION
        and existing.prompt_version == PROMPT_VERSION
        and existing.content_fingerprint == content_digest
        and existing.profile_fingerprint == profile_digest
    ):
        return False
    comparable = (
        "match_score",
        "summary",
        "recommendation",
        "evidence_level",
        "primary_match_direction",
        "analysis_status",
        "model",
        "filter_reasons",
        "refusal_reason",
        "error_code",
    )
    if any(getattr(existing, field_name) != values.get(field_name) for field_name in comparable):
        return False
    return (
        _json_text(existing.advantages) == _json_text(values["advantages"])
        and _json_text(existing.gaps) == _json_text(values["gaps"])
        and existing.score_breakdown == values["score_breakdown"]
        and existing.evidence == values["evidence"]
        and existing.matched_directions == values["matched_directions"]
    )


def _prepare_writes(
    storage: Storage,
    frozen: _Manifest,
    raw_reviews: Sequence[_RawReview],
    report: ReviewImportReport,
    replace_complete_job_ids: set[str],
) -> list[_PlannedWrite]:
    plans: list[_PlannedWrite] = []
    with storage.session() as session:
        for raw_review in raw_reviews:
            shape = _review_shape(raw_review.review, review_mode=frozen.review_mode)
            if isinstance(shape, str):
                _add_rejection(report, raw_review.job_id, shape, raw_review.source)
                continue
            decision, reason, raw_analysis = shape
            row = session.get(JobSnapshot, raw_review.job_id)
            if row is None:
                _add_rejection(report, raw_review.job_id, "job_not_in_current_database", raw_review.source)
                continue
            company = session.get(CompanySnapshot, row.company_id)
            state_error, current_payload, current_digest = _state_reason(
                row, company, frozen.jobs[raw_review.job_id]
            )
            if state_error is not None:
                _add_rejection(report, raw_review.job_id, state_error, raw_review.source)
                continue
            assert current_payload is not None
            assert current_digest is not None
            screening = screen_job(current_payload, frozen.profile)
            capture = assess_jd_capture(current_payload)
            payload: DeepSeekMatchPayload | None = None
            score: int | None = None
            recommendation: str | None = None
            score_values: dict[str, Any] | None = None
            persisted_status = "complete"
            if decision == "score":
                assert raw_analysis is not None
                validated = _validate_payload(raw_analysis)
                if isinstance(validated, str):
                    _add_rejection(report, raw_review.job_id, validated, raw_review.source)
                    continue
                payload = validated
                evidence_error = _validate_evidence(
                    payload,
                    jd_raw=current_payload.get("jd_raw"),
                    profile=frozen.profile,
                )
                if evidence_error is not None:
                    _add_rejection(report, raw_review.job_id, evidence_error, raw_review.source)
                    continue
                if frozen.review_mode == "score_only" and capture.incomplete:
                    _add_rejection(
                        report,
                        raw_review.job_id,
                        f"capture_not_complete:{capture.reason_code}",
                        raw_review.source,
                    )
                    continue
                if frozen.review_mode != "score_only" and not screening.eligible:
                    _add_rejection(
                        report,
                        raw_review.job_id,
                        f"screening_not_eligible:{screening.analysis_status.value}",
                        raw_review.source,
                    )
                    continue
                score, recommendation = _score(payload)
            else:
                if decision == "exclude":
                    evidence_error = _validate_exclude_reason(reason, current_payload)
                    if evidence_error is not None:
                        _add_rejection(report, raw_review.job_id, evidence_error, raw_review.source)
                        continue
                persisted_status = _non_score_status(decision, screening) or ""
                if not persisted_status:
                    _add_rejection(
                        report,
                        raw_review.job_id,
                        "exclude_requires_screening_status",
                        raw_review.source,
                    )
                    continue
            existing = session.get(JobAnalysisSnapshot, raw_review.job_id)
            if score is not None and recommendation is not None and payload is not None:
                score_values = _score_values(
                    payload, score, recommendation, model=frozen.review_model
                )
            if decision != "score" and _same_non_score_decision(
                existing,
                persisted_status=persisted_status,
                content_digest=current_digest,
                profile_digest=frozen.profile_fingerprint,
            ):
                report.reused += 1
                continue
            previous = frozen.jobs[raw_review.job_id].get("previous_analysis")
            previous_mapping = _plain_mapping(previous)
            replace_complete = False
            if existing is not None and existing.analysis_status == "complete":
                if score_values is not None and _same_luna_score(
                    existing,
                    content_digest=current_digest,
                    profile_digest=frozen.profile_fingerprint,
                    values=score_values,
                    model=frozen.review_model,
                ):
                    report.reused += 1
                    continue
                if raw_review.job_id in replace_complete_job_ids:
                    if not _previous_analysis_matches(existing, previous_mapping):
                        _add_rejection(
                            report,
                            raw_review.job_id,
                            "complete_replace_conflict",
                            raw_review.source,
                        )
                        continue
                    replace_complete = True
                elif existing.model == frozen.review_model:
                    _add_rejection(
                        report,
                        raw_review.job_id,
                        "new_score_conflict",
                        raw_review.source,
                    )
                    continue
                else:
                    report.reused += 1
                    continue
            analysis_source_ref = f"{row.source_ref or row.id}:analysis"
            if len(analysis_source_ref) > 512:
                _add_rejection(
                    report,
                    raw_review.job_id,
                    "analysis_source_ref_too_long",
                    raw_review.source,
                )
                continue
            plans.append(
                _PlannedWrite(
                    job_id=raw_review.job_id,
                    frozen_job=frozen.jobs[raw_review.job_id],
                    decision=decision,
                    reason=reason,
                    analysis=payload,
                    score=score,
                    recommendation=recommendation,
                    current_fingerprint=current_digest,
                    profile_fingerprint=frozen.profile_fingerprint,
                    persisted_status=persisted_status,
                    review_mode=frozen.review_mode,
                    review_model=frozen.review_model,
                    replace_complete=replace_complete,
                    previous_analysis=previous_mapping,
                )
            )
    return plans


def _lock_job(session: Any, job_id: str) -> JobSnapshot | None:
    statement = select(JobSnapshot).where(JobSnapshot.id == job_id).with_for_update()
    return session.execute(statement).scalar_one_or_none()


def _write_analysis(
    session: Any,
    plan: _PlannedWrite,
    profile: Mapping[str, Any],
) -> str:
    row = _lock_job(session, plan.job_id)
    if row is None:
        raise ReviewImportTransactionError(f"job_disappeared:{plan.job_id}")
    company = session.get(CompanySnapshot, row.company_id)
    state_error, current_payload, current_digest = _state_reason(row, company, plan.frozen_job)
    if state_error is not None:
        raise ReviewImportTransactionError(f"state_changed:{plan.job_id}:{state_error}")
    if current_payload is None or current_digest != plan.current_fingerprint:
        raise ReviewImportTransactionError(f"state_changed:{plan.job_id}:fingerprint")
    screening = screen_job(current_payload, profile)
    capture = assess_jd_capture(current_payload)
    if plan.decision == "score" and capture.incomplete:
        raise ReviewImportTransactionError(
            f"state_changed:{plan.job_id}:capture_not_complete:{capture.reason_code}"
        )
    if plan.decision == "score" and not screening.eligible and plan.review_mode != "score_only":
        raise ReviewImportTransactionError(
            f"state_changed:{plan.job_id}:screening_not_eligible"
        )
    if plan.decision != "score" and _non_score_status(plan.decision, screening) != plan.persisted_status:
        raise ReviewImportTransactionError(
            f"state_changed:{plan.job_id}:screening_status"
        )
    existing = session.get(JobAnalysisSnapshot, plan.job_id)
    if plan.decision != "score" and _same_non_score_decision(
        existing,
        persisted_status=plan.persisted_status,
        content_digest=current_digest,
        profile_digest=plan.profile_fingerprint,
    ):
        return "reused"

    if plan.decision == "score":
        if plan.analysis is None or plan.score is None or plan.recommendation is None:
            raise ReviewImportTransactionError(f"score_plan_invalid:{plan.job_id}")
        payload = plan.analysis
        values = _score_values(
            payload, plan.score, plan.recommendation, model=plan.review_model
        )
    else:
        values = {
            "match_score": None,
            "advantages": json.dumps([], ensure_ascii=False),
            "gaps": json.dumps([], ensure_ascii=False),
            "summary": plan.reason,
            "recommendation": "未评估",
            "score_breakdown": {},
            "evidence": [],
            "evidence_level": None,
            "matched_directions": [item.value for item in screening.matched_directions],
            "primary_match_direction": (
                screening.primary_match_direction.value
                if screening.primary_match_direction is not None
                else None
            ),
            "analysis_status": plan.persisted_status,
            "model": plan.review_model,
            "filter_reasons": [plan.reason],
            "refusal_reason": None,
            "error_code": None,
        }

    if existing is not None and existing.analysis_status == "complete":
        if plan.decision == "score" and _same_luna_score(
            existing,
            content_digest=current_digest,
                profile_digest=plan.profile_fingerprint,
                values=values,
                model=plan.review_model,
        ):
            return "reused"
        if not plan.replace_complete:
            if existing.model == plan.review_model:
                raise ReviewImportTransactionError(
                    f"new_score_conflict:{plan.job_id}"
                )
            return "reused"
        if not _previous_analysis_matches(existing, plan.previous_analysis):
            raise ReviewImportTransactionError(
                f"complete_replace_conflict:{plan.job_id}"
            )

    now = datetime.now(timezone.utc)
    row.match_score = plan.score if plan.decision == "score" else None
    row.updated_at = now
    if existing is None:
        existing = JobAnalysisSnapshot(
            job_id=plan.job_id,
            created_at=now,
            source=row.source,
            source_ref=f"{row.source_ref or row.id}:analysis",
        )
        session.add(existing)
    else:
        existing.source = row.source
        existing.source_ref = f"{row.source_ref or row.id}:analysis"
    for field_name, value in values.items():
        setattr(existing, field_name, value)
    existing.analysis_version = ANALYSIS_VERSION
    existing.prompt_version = PROMPT_VERSION
    existing.content_fingerprint = current_digest
    existing.profile_fingerprint = plan.profile_fingerprint
    existing.input_tokens = None
    existing.output_tokens = None
    existing.analyzed_at = now
    existing.updated_at = now
    return "written"


def import_reviewed_scores(
    storage: Storage,
    manifest: Mapping[str, Any] | _Manifest,
    result_documents: Mapping[str, Any] | Sequence[Any],
    *,
    apply: bool = False,
    manifest_base_dir: Path | None = None,
    replace_complete_job_ids: Sequence[str] = (),
    current_profile_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Validate and optionally import one or more Luna result documents.

    ``apply`` is intentionally false by default.  Validation rejects individual
    bad reviews while allowing unrelated valid reviews to be planned; an
    unexpected write error raises after the whole SQL transaction is rolled
    back.
    """

    frozen = manifest if isinstance(manifest, _Manifest) else _validate_manifest(manifest)
    if current_profile_fingerprint is not None:
        current_digest = _require_digest(
            current_profile_fingerprint,
            "current_profile_fingerprint",
        )
        if current_digest != frozen.profile_fingerprint:
            raise ReviewImportError("current_profile_fingerprint_mismatch")
    report = ReviewImportReport(dry_run=not apply)
    documents = _result_documents(result_documents)
    raw_reviews = _collect_reviews(frozen, documents, report)
    replace_ids = {str(item).strip() for item in replace_complete_job_ids if str(item).strip()}
    plans = _prepare_writes(storage, frozen, raw_reviews, report, replace_ids)
    if not apply:
        return report.as_dict()

    verify_manifest_backup(frozen, base_dir=manifest_base_dir)
    try:
        with storage.transaction(write=True) as session:
            for plan in plans:
                result = _write_analysis(session, plan, frozen.profile)
                if result == "reused":
                    report.reused += 1
                else:
                    report.written += 1
    except Exception as exc:
        report.written = 0
        if isinstance(exc, ReviewImportTransactionError):
            raise
        raise ReviewImportTransactionError("transaction_rolled_back") from exc
    return report.as_dict()


def import_reviewed_scores_from_files(
    storage: Storage,
    manifest_path: Path | str,
    result_paths: Sequence[Path | str],
    *,
    apply: bool = False,
    replace_complete_job_ids: Sequence[str] = (),
    current_profile_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Load a frozen manifest and result files, then run the importer."""

    resolved_manifest = Path(manifest_path).expanduser().resolve()
    manifest = load_json_file(resolved_manifest)
    documents = [(str(Path(path).expanduser().resolve()), load_json_file(path)) for path in result_paths]
    return import_reviewed_scores(
        storage,
        manifest,
        documents,
        apply=apply,
        manifest_base_dir=resolved_manifest.parent,
        replace_complete_job_ids=replace_complete_job_ids,
        current_profile_fingerprint=current_profile_fingerprint,
    )


__all__ = [
    "EXPECTED_MODEL",
    "Rejection",
    "ReviewImportError",
    "ReviewImportReport",
    "ReviewImportTransactionError",
    "import_reviewed_scores",
    "import_reviewed_scores_from_files",
    "load_json_file",
    "verify_manifest_backup",
]
