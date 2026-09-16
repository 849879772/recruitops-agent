from hashlib import sha256

import pytest

from packages.matching.models import AnalysisStatus
from packages.matching.rules import is_jd_incomplete, screen_job
from packages.recruitment_core.jd_capture import assess_jd_capture


SHORT_OFFICIAL_JD = "负责 C++ 服务开发，参与 Linux 环境下的测试与发布。"


def _evidence(jd: str = SHORT_OFFICIAL_JD) -> dict[str, object]:
    return {
        "status": "complete",
        "method": "rendered_detail",
        "source_url": "https://example.test/jobs/short-official",
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(jd.encode("utf-8")).hexdigest(),
    }


def _job(**overrides: object) -> dict[str, object]:
    job: dict[str, object] = {
        "id": "short-official",
        "title": "C++软件开发工程师",
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": "formal",
        "jd_raw": SHORT_OFFICIAL_JD,
        "capture_evidence": _evidence(),
    }
    job.update(overrides)
    return job


def test_short_official_jd_with_complete_capture_evidence_passes_without_sections() -> None:
    assert "岗位职责" not in SHORT_OFFICIAL_JD
    assert "任职要求" not in SHORT_OFFICIAL_JD

    assessment = assess_jd_capture(_job())
    screening = screen_job(_job())

    assert assessment.complete is True
    assert is_jd_incomplete(_job()) is False
    assert screening.eligible is True
    assert screening.analysis_status is AnalysisStatus.ELIGIBLE


@pytest.mark.parametrize(
    ("name", "evidence", "jd_raw"),
    [
        ("empty", {}, SHORT_OFFICIAL_JD),
        ("missing", None, SHORT_OFFICIAL_JD),
        ("hash_changed", _evidence("different captured content"), SHORT_OFFICIAL_JD),
        ("identity_unknown", {**_evidence(), "identity_verified": False}, SHORT_OFFICIAL_JD),
        (
            "remaining_controls",
            {**_evidence(), "remaining_controls": ["load-more"]},
            SHORT_OFFICIAL_JD,
        ),
    ],
)
def test_capture_evidence_failures_block_jd_gate(
    name: str, evidence: object, jd_raw: str
) -> None:
    job = _job(capture_evidence=evidence, jd_raw=jd_raw)

    assessment = assess_jd_capture(job)
    screening = screen_job(job)

    assert assessment.complete is False, name
    assert is_jd_incomplete(job) is True
    assert screening.eligible is False
    assert screening.analysis_status is AnalysisStatus.JD_INCOMPLETE
