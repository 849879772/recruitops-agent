"""Deterministic selection and validation for catalog JD repair.

This module deliberately does not read or write a database.  It keeps the
repair script's safety gates close to the hydration contract so a report can
be reviewed before any future import is attempted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import re
from typing import Any
from urllib.parse import urlsplit

from .crawlers.feishu import feishu_project_path
from .jd_capture import assess_jd_capture


_DETAIL_ROUTE_RE = re.compile(r"/position/[^/]+/detail(?:/|$)", re.I)
_FEISHU_HOST_RE = re.compile(r"(?:^|\.)(?:jobs\.feishu\.cn|mioffice\.cn)$", re.I)
_SOURCE_TENANT_RE = re.compile(r"^(?:feishu|lark):([^:/]+(?::\d+)?)", re.I)
_DUTY_RE = re.compile(
    r"职位描述|岗位描述|职位职责|岗位职责|工作职责|工作内容|responsibilities",
    re.I,
)
_REQUIREMENT_RE = re.compile(
    r"任职要求|岗位要求|任职资格|招聘要求|qualifications|requirements",
    re.I,
)
_TERMINAL_CHARS = frozenset(
    "。！？!?；;:：)]）】]}”’\"'"
)

# These are source-side caps observed in crawler adapters.  A cap is only
# repair evidence when the text also looks cut at a clause boundary; length
# alone is intentionally insufficient.
KNOWN_JD_LIMITS = {
    "feishu": 500,
    "lark": 500,
    "xiaomi": 500,
    "lenovo": 500,
    "bilibili": 200,
    "byd": 800,
}


@dataclass(frozen=True, slots=True)
class ProvenanceCheck:
    """Official host and campaign binding evidence for one catalog row."""

    ok: bool
    status: str
    evidence: tuple[str, ...] = ()
    failure_reason: str = ""


def content_sha256(value: object) -> str:
    """Return a stable hash for the exact stored UTF-8 content."""

    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _host(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").casefold()


def _project_token(value: object) -> str:
    """Normalize a trusted DB project id or a Feishu route prefix."""

    if isinstance(value, Mapping):
        for key in ("id", "projectId", "project_id", "campaignId", "campaign_id"):
            if value.get(key) not in (None, ""):
                return _project_token(value[key])
        return ""
    raw = _text(value)
    if not raw:
        return ""
    if "://" in raw or raw.startswith("/"):
        project = feishu_project_path(raw).strip("/")
        # Feishu's /<project>/m/ entry is a mobile landing path for the same
        # project.  Canonicalize only on a known Feishu host; a generic /m/
        # path must not become evidence for a shared or unrelated tenant.
        if _FEISHU_HOST_RE.search(_host(raw)):
            project = re.sub(r"(?:^|/)m$", "", project, flags=re.I).strip("/")
        return project.casefold()
    if raw.casefold().startswith(("feishu:", "lark:")):
        raw = raw.split(":", 1)[1]
    return raw.strip("/").casefold()


def _feishu_project_bindings(job: Mapping[str, Any]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return trusted DB bindings and observed URL bindings in priority order."""

    trusted: list[tuple[str, str]] = []
    for field in (
        "recruitment_campaign_id",
        "platform_project_id",
        "observed_project_id",
        "source_project_id",
    ):
        token = _project_token(job.get(field))
        if token:
            trusted.append((field, token))

    observed: list[tuple[str, str]] = []
    for field in (
        "company_campus_url",
        "campus_url",
        "careers_url",
        "campaign_url",
        "source_url",
        "source_list_url",
        "list_url",
        "resolved_source_url",
    ):
        token = _project_token(job.get(field))
        if token:
            observed.append((field, token))
    return trusted, observed


def _is_feishu(job: Mapping[str, Any]) -> bool:
    platform = _text(job.get("source_platform") or job.get("crawler_key")).casefold()
    detail_host = _host(job.get("detail_url") or job.get("jd_url"))
    return (
        platform in {"feishu", "lark", "xiaomi"}
        or bool(_FEISHU_HOST_RE.search(detail_host))
    )


def _source_limit(job: Mapping[str, Any]) -> int | None:
    platform = _text(job.get("source_platform") or job.get("crawler_key")).casefold()
    if platform in KNOWN_JD_LIMITS:
        return KNOWN_JD_LIMITS[platform]
    if _is_feishu(job):
        return KNOWN_JD_LIMITS["feishu"]
    return None


def truncation_evidence(job: Mapping[str, Any]) -> tuple[str, ...]:
    """Return conservative evidence that a stored JD hit an adapter cap."""

    raw = str(job.get("jd_raw") or "")
    limit = _source_limit(job)
    if limit is None or len(raw) != limit:
        return ()

    stripped = raw.rstrip()
    if not stripped:
        return ()
    reasons: list[str] = []
    if stripped[-1] not in _TERMINAL_CHARS:
        reasons.append("ends_mid_clause")
    duty = bool(_DUTY_RE.search(raw))
    requirement = bool(_REQUIREMENT_RE.search(raw))
    if duty != requirement and stripped[-1] not in _TERMINAL_CHARS:
        reasons.append("section_unbalanced_at_limit")
    return tuple(f"source_limit_{limit}:{reason}" for reason in reasons)


def validate_provenance(job: Mapping[str, Any]) -> ProvenanceCheck:
    """Bind a detail request to its catalog company and campaign scope."""

    detail_url = str(job.get("detail_url") or job.get("jd_url") or "").strip()
    parsed = urlsplit(detail_url)
    detail_host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not detail_host:
        return ProvenanceCheck(False, "invalid_detail_url", failure_reason="invalid_detail_url")
    if str(job.get("link_kind") or "").casefold() == "list":
        return ProvenanceCheck(False, "list_url", failure_reason="list_url_not_allowed")

    expected_hosts: list[str] = []
    source_tenant = str(job.get("source_tenant") or "").strip()
    tenant_match = _SOURCE_TENANT_RE.match(source_tenant)
    if tenant_match:
        expected_hosts.append(tenant_match.group(1).casefold())
    campus_url = str(
        job.get("company_campus_url")
        or job.get("campus_url")
        or job.get("careers_url")
        or ""
    ).strip()
    campus_host = _host(campus_url)
    if campus_host:
        expected_hosts.append(campus_host)
    expected_hosts = list(dict.fromkeys(expected_hosts))
    if len(expected_hosts) > 1:
        return ProvenanceCheck(
            False,
            "host_mismatch",
            tuple(f"expected_host:{host}" for host in expected_hosts),
            "company_host_mismatch",
        )
    if not expected_hosts:
        return ProvenanceCheck(False, "company_unbound", failure_reason="company_source_unbound")
    if detail_host not in expected_hosts:
        return ProvenanceCheck(
            False,
            "host_mismatch",
            evidence=(f"detail_host:{detail_host}", f"expected_host:{expected_hosts[0]}"),
            failure_reason="company_host_mismatch",
        )

    evidence = [f"detail_host:{detail_host}", f"company_host:{detail_host}"]
    if source_tenant:
        evidence.append(f"source_tenant:{source_tenant}")

    if _is_feishu(job):
        if not _DETAIL_ROUTE_RE.search(parsed.path):
            return ProvenanceCheck(False, "not_detail_route", tuple(evidence), "feishu_detail_route_missing")
        detail_project = _project_token(detail_url)
        trusted_bindings, observed_bindings = _feishu_project_bindings(job)
        bindings = trusted_bindings or observed_bindings
        if not detail_project or not bindings:
            return ProvenanceCheck(
                False,
                "campaign_unverified",
                tuple(evidence),
                "feishu_campaign_scope_unverified",
            )
        expected_projects = {project for _, project in bindings}
        evidence.extend(f"{field}:{project}" for field, project in bindings)
        evidence.append(f"detail_project:{detail_project}")
        if len(expected_projects) != 1 or detail_project not in expected_projects:
            return ProvenanceCheck(
                False,
                "campaign_mismatch",
                tuple(evidence),
                "feishu_campaign_scope_mismatch",
            )
        evidence.append(f"project:{detail_project}")
        return ProvenanceCheck(True, "company_and_campaign_bound", tuple(evidence))

    return ProvenanceCheck(True, "company_bound", tuple(evidence))


def repair_decision(job: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one row without performing I/O or treating every 500 as bad."""

    try:
        cohort = int(job.get("cohort") or 0)
    except (TypeError, ValueError):
        cohort = 0
    if cohort != 2027 or _text(job.get("cohort_status")).casefold() != "confirmed":
        return {"selected": False, "reason": "cohort_ineligible", "evidence": []}
    provenance = validate_provenance(job)
    if not provenance.ok:
        return {
            "selected": False,
            "reason": provenance.failure_reason,
            "evidence": list(provenance.evidence),
            "provenance": provenance.status,
        }
    evidence = list(truncation_evidence(job))
    incomplete = assess_jd_capture(job).incomplete
    if not evidence and not incomplete:
        return {
            "selected": False,
            "reason": "stored_jd_not_proven_incomplete",
            "evidence": list(provenance.evidence),
            "provenance": provenance.status,
        }
    if evidence:
        reason = "source_cap_truncation"
    else:
        reason = "stored_jd_incomplete"
    return {
        "selected": True,
        "reason": reason,
        "evidence": [*provenance.evidence, *evidence],
        "provenance": provenance.status,
    }


def validate_candidate(
    job: Mapping[str, Any],
    hydration: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Validate a fetched candidate before it can be considered importable."""

    def field(name: str, default: Any = "") -> Any:
        if isinstance(hydration, Mapping):
            return hydration.get(name, default)
        return getattr(hydration, name, default)

    candidate = str(field("detail", "") or "")
    status = _text(field("status", ""))
    source = _text(field("source", ""))
    detail_url = str(
        field("detail_url", "")
        or job.get("detail_url")
        or job.get("jd_url")
        or ""
    ).strip()
    identity_status = _text(field("identity_status", "")).casefold()
    identity_evidence = tuple(str(item) for item in (field("identity_evidence", ()) or ()))
    capture = field("capture_evidence", {}) or {}
    verified_list_interaction = bool(
        str(job.get("link_kind") or "").casefold() == "list"
        and _text(capture.get("status")).casefold() == "complete"
        and _text(capture.get("method")).casefold().startswith("detail_interaction:")
        and capture.get("identity_verified") is True
        and capture.get("terminal_observed") is True
        and not list(capture.get("remaining_controls") or [])
        and capture.get("content_sha256") == content_sha256(candidate)
    )
    reasons: list[str] = []
    provenance_job = {**dict(job), "detail_url": detail_url}
    if verified_list_interaction:
        provenance_job["link_kind"] = "detail_interaction"
    provenance = validate_provenance(provenance_job)
    if status != "complete":
        reasons.append(f"hydration_status:{status or 'missing'}")
    if not candidate:
        reasons.append("candidate_jd_empty")
    if identity_status not in {"matched", "request_bound"} or not identity_evidence:
        reasons.append("identity_unverified")
    if not provenance.ok:
        reasons.append(provenance.failure_reason or provenance.status)
    if (
        source.casefold() in {"configured_page", "configured_page_render"}
        and not verified_list_interaction
    ):
        reasons.append("list_page_source_not_allowed")
    if candidate and assess_jd_capture({**dict(job), "jd_raw": candidate, "capture_evidence": capture}).incomplete:
        reasons.append("candidate_jd_incomplete")
    passed = not reasons
    return {
        "passed": passed,
        "status": status,
        "source": source,
        "detail_url": detail_url,
        "candidate_chars": len(candidate),
        "candidate_sha256": content_sha256(candidate) if candidate else "",
        "identity_status": identity_status,
        "identity_evidence": list(identity_evidence),
        "capture_evidence": capture,
        "provenance_status": provenance.status,
        "provenance_evidence": list(provenance.evidence),
        "failure_reasons": reasons,
    }


__all__ = [
    "KNOWN_JD_LIMITS",
    "ProvenanceCheck",
    "content_sha256",
    "repair_decision",
    "truncation_evidence",
    "validate_candidate",
    "validate_provenance",
]
