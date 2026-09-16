"""Validate crawler-produced capture evidence, not the richness of a JD."""

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any


@dataclass(frozen=True)
class JdCaptureAssessment:
    complete: bool
    reason_code: str
    reason: str

    @property
    def incomplete(self) -> bool:
        return not self.complete


def assess_jd_capture(job: Any) -> JdCaptureAssessment:
    def field(name: str, default: Any = None) -> Any:
        return job.get(name, default) if isinstance(job, Mapping) else getattr(job, name, default)

    detail = str(field("jd_raw") or "").strip()
    evidence = field("capture_evidence")
    if not isinstance(evidence, Mapping) or not evidence:
        return JdCaptureAssessment(False, "capture_unverified", "Official detail capture has not been verified.")
    if evidence.get("status") != "complete":
        return JdCaptureAssessment(False, "capture_incomplete", "Official detail capture is incomplete or unknown.")
    if evidence.get("identity_verified") is not True:
        return JdCaptureAssessment(False, "identity_unverified", "Detail identity has not been verified.")
    if evidence.get("terminal_observed") is not True or evidence.get("remaining_controls"):
        return JdCaptureAssessment(False, "detail_not_terminated", "Detail content still has unresolved loading controls.")
    if not detail or not evidence.get("source_url") or not evidence.get("method"):
        return JdCaptureAssessment(False, "capture_source_missing", "Captured detail or its source is missing.")
    if evidence.get("content_sha256") != sha256(detail.encode("utf-8")).hexdigest():
        return JdCaptureAssessment(False, "capture_content_changed", "Capture evidence does not match the stored detail.")
    return JdCaptureAssessment(True, "capture_complete", "Captured official detail matches its evidence.")
