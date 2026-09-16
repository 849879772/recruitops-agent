from hashlib import sha256
from types import SimpleNamespace

import pytest

from packages.matching.title_policy import stored_detail_retry_required


def receipt(body):
    return {
        "status": "complete",
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "source_url": "https://careers.example/jobs/1",
        "method": "official_api",
        "content_sha256": sha256(body.strip().encode()).hexdigest(),
    }


@pytest.mark.parametrize("body", [None, "", " \n\t"])
def test_empty_body_requires_retry_even_if_status_says_complete(body):
    assert stored_detail_retry_required({"jd_raw": body, "capture_status": "complete"})


@pytest.mark.parametrize("status", [None, "", "unknown", "complete"])
def test_existing_legacy_body_is_not_failure_due_to_missing_metadata(status):
    assert not stored_detail_retry_required(
        {"jd_raw": "Short official JD", "capture_status": status}
    )


@pytest.mark.parametrize("status", ["failed", "pending", "incomplete", "error"])
def test_explicit_unfinished_capture_with_body_requires_retry(status):
    assert stored_detail_retry_required({"jd_raw": "Partial body", "capture_status": status})


def test_verified_body_wins_over_stale_failure_flags():
    body = "Official JD"
    assert not stored_detail_retry_required(
        {"jd_raw": body, "capture_evidence": receipt(body), "capture_status": "failed"}
    )


@pytest.mark.parametrize("change", [
    {"content_sha256": "wrong"},
    {"terminal_observed": False},
    {"identity_verified": False},
    {"status": "incomplete"},
])
def test_invalid_existing_receipt_is_a_retry_reason(change):
    body = "Official JD"
    assert stored_detail_retry_required(
        {"jd_raw": body, "capture_evidence": {**receipt(body), **change}}
    )


def test_failure_reason_and_snapshot_objects_are_supported():
    assert stored_detail_retry_required(
        SimpleNamespace(jd_raw="Old partial JD", capture_failure_reason="fetch_failed")
    )
    assert not stored_detail_retry_required(SimpleNamespace(jd_raw="Legacy JD"))


@pytest.mark.parametrize("score,valid", [
    (0, True), (100, True), (83.5, True), ("72", True),
    (-1, False), (101, False), (float("nan"), False),
    (float("inf"), False), (float("-inf"), False),
    (True, False), (False, False), (None, False), ("", False),
])
def test_existing_retry_score_validation_does_not_clamp_or_accept_nonfinite(score, valid):
    from packages.pipeline.daily import _existing_score_is_valid

    stored = SimpleNamespace(job=SimpleNamespace(match_score=score), analysis=None)
    assert _existing_score_is_valid(stored) is valid


def test_existing_retry_can_preserve_a_complete_analysis_without_job_score():
    from packages.pipeline.daily import _existing_score_is_valid

    stored = SimpleNamespace(
        job=SimpleNamespace(match_score=None),
        analysis=SimpleNamespace(analysis_status="complete", match_score=0),
    )
    assert _existing_score_is_valid(stored)
    stored.analysis.analysis_status = "pending"
    assert not _existing_score_is_valid(stored)
