"""Refresh non-model catalog screening with the current deterministic rules."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.candidate_profile import load_candidate_profile  # noqa: E402
from packages.config import get_settings  # noqa: E402
from packages.matching.models import AnalysisStatus  # noqa: E402
from packages.matching.review_import import (  # noqa: E402
    load_json_file,
    verify_manifest_backup,
)
from packages.matching.rules import (  # noqa: E402
    content_fingerprint,
    profile_fingerprint,
    screen_job,
)
from packages.storage import CompanySnapshot, JobAnalysisSnapshot, JobSnapshot, Storage  # noqa: E402


_RULE_FIELDS = (
    "analysis_status",
    "filter_reasons",
    "matched_directions",
    "primary_match_direction",
    "content_fingerprint",
    "profile_fingerprint",
    "summary",
    "recommendation",
)
_SUMMARY = {
    AnalysisStatus.ELIGIBLE.value: "通过当前确定性筛选，尚未进行匹配评分",
    AnalysisStatus.COHORT_UNCONFIRMED.value: "届别待确认，不进行匹配评分",
    AnalysisStatus.INTERNSHIP.value: "实习岗位不进行校招匹配评分",
    AnalysisStatus.DOCTORATE_ONLY.value: "博士限定岗位不进入匹配评分",
    AnalysisStatus.JD_INCOMPLETE.value: "JD不完整，待补全后评分",
    AnalysisStatus.DIRECTION_OUT.value: "岗位未命中配置的目标方向",
}


class RescreenError(ValueError):
    """Raised when a rescreen input or guarded write is unsafe."""


class RescreenTransactionError(RescreenError):
    """Raised after a rescreen transaction has been rolled back."""


@dataclass(frozen=True)
class _Plan:
    job_id: str
    content_digest: str
    values: Mapping[str, Any]


@dataclass
class _Report:
    dry_run: bool
    planned: int = 0
    written: int = 0
    reused: int = 0
    skipped: int = 0
    conflicts: int = 0
    rule_status_counts: Counter[str] = field(default_factory=Counter)
    skipped_reasons: Counter[str] = field(default_factory=Counter)
    conflict_reasons: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "planned": self.planned,
            "written": self.written,
            "reused": self.reused,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "rule_status_counts": dict(sorted(self.rule_status_counts.items())),
            "skipped_reasons": dict(sorted(self.skipped_reasons.items())),
            "conflict_reasons": dict(sorted(self.conflict_reasons.items())),
        }


def _status_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().casefold()


def _model_present(analysis: JobAnalysisSnapshot | None) -> bool:
    return analysis is not None and bool(str(analysis.model or "").strip())


def _job_payload(row: JobSnapshot, company: CompanySnapshot | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "company": company.name if company is not None else row.company_id,
        "company_id": row.company_id,
        "title": row.title,
        "city": row.city,
        "detail_url": row.detail_url,
        "jd_raw": row.jd_raw,
        "capture_evidence": row.capture_evidence or {},
        "cohort": row.cohort,
        "cohort_status": row.cohort_status,
        "batch": row.batch,
    }


def _summary(status: str) -> str:
    return _SUMMARY.get(status, f"当前确定性规则状态：{status or 'unknown'}")


def _rule_values(
    job: Mapping[str, Any],
    profile: Any,
    *,
    current_content_digest: str,
    current_profile_digest: str,
) -> dict[str, Any]:
    screening = screen_job(job, profile)
    status = screening.analysis_status.value
    return {
        "analysis_status": status,
        "filter_reasons": list(screening.reasons),
        "matched_directions": [item.value for item in screening.matched_directions],
        "primary_match_direction": (
            screening.primary_match_direction.value
            if screening.primary_match_direction is not None
            else None
        ),
        "content_fingerprint": current_content_digest,
        "profile_fingerprint": current_profile_digest,
        "summary": _summary(status),
        "recommendation": "未评估",
    }


def _same_rule_values(analysis: JobAnalysisSnapshot, values: Mapping[str, Any]) -> bool:
    for field_name in _RULE_FIELDS:
        actual = getattr(analysis, field_name)
        if field_name in {"filter_reasons", "matched_directions"}:
            actual = list(actual or [])
        if actual != values[field_name]:
            return False
    return True


def _add_skipped(report: _Report, reason: str) -> None:
    report.skipped += 1
    report.skipped_reasons[reason] += 1


def _add_conflict(report: _Report, reason: str) -> None:
    report.conflicts += 1
    report.conflict_reasons[reason] += 1


def _scan(
    session: Any,
    profile: Any,
    profile_digest: str,
    report: _Report,
) -> list[_Plan]:
    plans: list[_Plan] = []
    rows = session.execute(select(JobSnapshot).order_by(JobSnapshot.id)).scalars()
    for row in rows:
        analysis = session.get(JobAnalysisSnapshot, row.id)
        if _model_present(analysis):
            _add_skipped(report, "model_present")
            continue
        if analysis is not None and _status_value(analysis.analysis_status) == AnalysisStatus.COMPLETE.value:
            _add_skipped(report, "complete")
            continue

        company = session.get(CompanySnapshot, row.company_id)
        job = _job_payload(row, company)
        current_digest = content_fingerprint(job)
        screening = screen_job(job, profile)
        report.rule_status_counts[screening.analysis_status.value] += 1
        if (
            (analysis is not None and analysis.match_score is not None)
            or row.match_score is not None
        ):
            _add_conflict(report, "match_score_present")
            continue

        values = _rule_values(
            job,
            profile,
            current_content_digest=current_digest,
            current_profile_digest=profile_digest,
        )
        report.planned += 1
        if analysis is not None and _same_rule_values(analysis, values):
            report.reused += 1
            continue
        plans.append(_Plan(row.id, current_digest, values))
    return plans


def _lock_job(session: Any, job_id: str) -> JobSnapshot | None:
    statement = (
        select(JobSnapshot)
        .where(JobSnapshot.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one_or_none()


def _lock_analysis(session: Any, job_id: str) -> JobAnalysisSnapshot | None:
    statement = (
        select(JobAnalysisSnapshot)
        .where(JobAnalysisSnapshot.job_id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one_or_none()


def _new_analysis(row: JobSnapshot, values: Mapping[str, Any]) -> JobAnalysisSnapshot:
    return JobAnalysisSnapshot(
        job_id=row.id,
        source=row.source,
        source_ref=f"{row.source_ref or row.id}:analysis",
        match_score=None,
        advantages=json.dumps([], ensure_ascii=False),
        gaps=json.dumps([], ensure_ascii=False),
        summary=values["summary"],
        recommendation=values["recommendation"],
        score_breakdown={},
        evidence=[],
        evidence_level=None,
        matched_directions=list(values["matched_directions"]),
        primary_match_direction=values["primary_match_direction"],
        analysis_status=values["analysis_status"],
        model=None,
        filter_reasons=list(values["filter_reasons"]),
        content_fingerprint=values["content_fingerprint"],
        profile_fingerprint=values["profile_fingerprint"],
    )


def _apply_plan(
    session: Any,
    plan: _Plan,
    profile: Any,
    profile_digest: str,
    report: _Report,
) -> str:
    row = _lock_job(session, plan.job_id)
    if row is None:
        raise RescreenTransactionError(f"job_disappeared:{plan.job_id}")
    company = session.get(CompanySnapshot, row.company_id)
    job = _job_payload(row, company)
    current_digest = content_fingerprint(job)
    if current_digest != plan.content_digest:
        raise RescreenTransactionError(f"state_changed:{plan.job_id}:content_fingerprint")

    analysis = _lock_analysis(session, row.id)
    if _model_present(analysis):
        _add_skipped(report, "model_present_after_lock")
        return "skipped"
    if analysis is not None and _status_value(analysis.analysis_status) == AnalysisStatus.COMPLETE.value:
        _add_skipped(report, "complete_after_lock")
        return "skipped"
    if (
        (analysis is not None and analysis.match_score is not None)
        or row.match_score is not None
    ):
        _add_conflict(report, "match_score_present_after_lock")
        return "conflict"

    values = _rule_values(
        job,
        profile,
        current_content_digest=current_digest,
        current_profile_digest=profile_digest,
    )
    if analysis is not None and _same_rule_values(analysis, values):
        return "reused"
    if analysis is None:
        session.add(_new_analysis(row, values))
    else:
        for field_name in _RULE_FIELDS:
            setattr(analysis, field_name, values[field_name])
    return "written"


def _validate_profile_manifest(manifest: Mapping[str, Any], profile: Any) -> None:
    expected = manifest.get("profile_fingerprint")
    if not isinstance(expected, str) or len(expected) != 64:
        raise RescreenError("manifest_profile_fingerprint_invalid")
    manifest_profile = manifest.get("profile")
    if not isinstance(manifest_profile, Mapping):
        raise RescreenError("manifest_profile_invalid")
    if profile_fingerprint(manifest_profile) != expected:
        raise RescreenError("manifest_profile_fingerprint_mismatch")
    if profile_fingerprint(profile) != expected:
        raise RescreenError("current_profile_fingerprint_mismatch")


def rescreen_catalog(
    storage: Storage,
    profile: Any,
    *,
    apply: bool = False,
    manifest: Mapping[str, Any] | None = None,
    manifest_base_dir: Path | None = None,
) -> dict[str, Any]:
    """Plan or apply a deterministic rescreen against the current catalog."""

    if apply and manifest is None:
        raise RescreenError("apply_manifest_required")
    profile_digest = profile_fingerprint(profile)
    if manifest is not None:
        _validate_profile_manifest(manifest, profile)
        if apply:
            verify_manifest_backup(manifest, base_dir=manifest_base_dir)

    report = _Report(dry_run=not apply)
    try:
        with storage.transaction(write=apply) as session:
            plans = _scan(session, profile, profile_digest, report)
            if apply:
                for plan in plans:
                    outcome = _apply_plan(session, plan, profile, profile_digest, report)
                    if outcome == "written":
                        report.written += 1
                    elif outcome == "reused":
                        report.reused += 1
    except RescreenTransactionError:
        report.written = 0
        raise
    except Exception as exc:
        report.written = 0
        raise RescreenTransactionError("transaction_rolled_back") from exc
    return report.as_dict()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="existing frozen catalog manifest; required with --apply",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy database URL; defaults to RECRUITOPS_DATABASE_URL or settings",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="plan only (default)")
    mode.add_argument("--apply", action="store_true", help="write one guarded transaction")
    return parser


def _load_manifest(path: Path | None) -> tuple[Mapping[str, Any] | None, Path | None]:
    if path is None:
        return None, None
    resolved = path.expanduser().resolve()
    value = load_json_file(resolved)
    if not isinstance(value, Mapping):
        raise RescreenError("manifest_not_object")
    return value, resolved


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    try:
        manifest, manifest_path = _load_manifest(args.manifest)
        if args.apply and manifest is None:
            raise RescreenError("apply_manifest_required")
        profile = load_candidate_profile(Path(settings.candidate_profile_config))
        database_url = (
            args.database_url
            or os.environ.get("RECRUITOPS_DATABASE_URL")
            or settings.database_url
        )
        storage = Storage.from_url(database_url)
        try:
            report = rescreen_catalog(
                storage,
                profile,
                apply=args.apply,
                manifest=manifest,
                manifest_base_dir=manifest_path.parent if manifest_path else None,
            )
        finally:
            storage.engine.dispose()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
