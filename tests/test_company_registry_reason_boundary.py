import pytest

from packages.discovery.company_registry import CompanySourceRegistry
from packages.storage import Storage


@pytest.mark.parametrize("code,expected", [
    ("tplink_fetch_failed:Error:" + "browser missing\n" * 1000 + "END", "tplink_fetch_failed"),
    ("request_error:TimeoutError:" + "timeout " * 100 + "END", "request_error"),
    ("unstructured failure " * 100 + "END", "source_attempt_failed"),
    ("x" * 129, "source_attempt_failed"),
    ("x" * 128, "x" * 128),
    ("request_error:TimeoutError", "request_error"),
    ("tplink_fetch_failed:Error:missing", "tplink_fetch_failed"),
    ("timeout", "timeout"),
    ("", ""),
], ids=["tplink-long", "timeout-long", "unstructured", "129", "128",
        "legacy-short", "exception-short", "timeout", "empty"])
@pytest.mark.parametrize("detail_mode", ["empty", "same", "separate"])
def test_legacy_failure_is_recorded_with_full_details(tmp_path, code, expected, detail_mode):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'reasons.db'}", initialize=True)
    registry = CompanySourceRegistry(storage)
    row = registry.upsert_source(
        source="fixture", source_record_id="one", company_name="Fixture",
        source_url="https://example.test/source", entry_url="https://example.test/jobs",
    )
    registry.record_attempt(row["id"], status="complete", job_count=3, pagination_complete=True)
    detail = {"empty": "", "same": code, "separate": "diagnostic\n" * 1100 + "END"}[detail_mode]
    result = registry.record_attempt(
        row["id"], status="failed", reason_code=code, reason=detail,
        failure_stage="listing", pagination_complete=False,
    )
    assert result["reason_code"] == expected
    assert len(result["reason_code"]) <= 128
    expected_detail = detail
    if code != expected:
        if code not in expected_detail:
            expected_detail = f"{expected_detail}\n{code}" if expected_detail else code
    if len(expected_detail) > 10000:
        marker = "\n[truncated: reason exceeds 10000 characters]"
        assert result["reason"] == expected_detail[:10000 - len(marker)] + marker
    else:
        assert result["reason"] == expected_detail
    assert len(result["reason"]) <= 10000
    attempt = next(item for item in result["attempts"] if item["status"] == "failed")
    assert attempt["reason"] == result["reason"]
    assert attempt["reason_code"] == expected
    assert result["last_success_job_count"] == 3
    assert result["pagination_complete"] is False
    assert result["attempts_total"] == 2
    # A failed source must not prevent subsequent sources from being recorded.
    other = registry.upsert_source(
        source="fixture", source_record_id="two", company_name="Other",
        source_url="https://example.test/source", entry_url="https://example.test/jobs",
    )
    assert registry.record_attempt(other["id"], status="complete")["status"] == "complete"


@pytest.mark.parametrize("status", ["pending", "running", "complete", "partial"])
@pytest.mark.parametrize("code,expected", [
    ("complete: no more pages", "complete: no more pages"),
    ("\u5b8c\u6210", "\u5b8c\u6210"),
    ("successful observation " * 10, "source_attempt_note"),
])
def test_non_failure_labels_do_not_become_failure_codes(tmp_path, status, code, expected):
    registry = CompanySourceRegistry(
        Storage.from_url(f"sqlite:///{tmp_path / 'labels.db'}", initialize=True)
    )
    row = registry.upsert_source(
        source="fixture", source_record_id="label", company_name="Fixture",
        source_url="https://example.test/source", entry_url="https://example.test/jobs",
    )
    result = registry.record_attempt(row["id"], status=status, reason_code=code)
    assert result["status"] == status
    assert result["reason_code"] == expected
    if len(code) > 128:
        assert result["reason"] == code.strip()


@pytest.mark.parametrize("size", [9999, 10000, 10001])
def test_reason_input_limit_is_non_throwing_and_explicit(size):
    from packages.discovery.company_registry import _attempt_reason

    code, detail = _attempt_reason("timeout", "x" * size, "failed")
    assert code == "timeout"
    assert len(detail) == min(size, 10000)
    assert ("[truncated:" in detail) == (size > 10000)
