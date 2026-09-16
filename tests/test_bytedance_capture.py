from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256

import pytest

import packages.recruitment_core.crawlers.bytedance as bytedance_module
from packages.recruitment_core.api_capture import (
    OfficialApiResponseContext,
    build_official_api_capture_evidence,
    native_post_id,
    normalize_official_jd,
)
from packages.recruitment_core.crawlers.bytedance import ByteDanceCrawler
from packages.recruitment_core.jd_capture import assess_jd_capture
from packages.recruitment_core.job_details import fetch_full_job_description_result


API_URL = "https://jobs.bytedance.com/api/v1/search/job/posts"
LIST_URL = "https://jobs.bytedance.com/campus/position"
REQUEST = {"subject_id_list": [101, 202, 303], "offset": 0, "limit": 100}


def _row(
    post_id: object = "1234567890123456789",
    *,
    title: str = "AI 工程师",
    description: object = "负责模型服务开发。",
    requirement: object = "熟悉 Python。",
    **extra,
) -> dict:
    row = {
        "id": post_id,
        "title": title,
        "city_list": [{"name": "北京"}],
        "description": description,
        "requirement": requirement,
    }
    row.update(extra)
    return row


def _response_text(*rows: dict, total: int | None = None, **data) -> str:
    payload_data = {
        "count": len(rows) if total is None else total,
        "job_post_list": list(rows),
        **data,
    }
    return json.dumps({"code": 0, "data": payload_data}, ensure_ascii=False)


def _context(row: dict, *, response_text: str | None = None, url: str = API_URL):
    return OfficialApiResponseContext.from_captured_response(
        response_url=url,
        request_method="POST",
        response_status=200,
        request_payload=REQUEST,
        response_text=response_text or _response_text(row),
    )


def _crawler() -> ByteDanceCrawler:
    return ByteDanceCrawler("字节跳动", LIST_URL)


def test_verified_list_api_keeps_jd_over_12000_chars_and_receipt_hash() -> None:
    description = "D" * 12_001
    requirement = "R" * 37
    row = _row(description=description, requirement=requirement)
    context = _context(row)

    job = _crawler()._parse_api_job(row, response_context=context)

    expected = f"职位描述\n{description}\n任职要求\n{requirement}"
    assert job["jd_raw"] == expected
    assert len(job["jd_raw"]) > 12_000
    assert job["source_job_id"] == "1234567890123456789"
    assert job["native_job_id"] == "1234567890123456789"
    evidence = job["capture_evidence"]
    assert evidence["status"] == "complete"
    assert evidence["source_url"] == job["detail_url"]
    assert evidence["api_source_url"] == API_URL
    assert f"native_id:{row['id']}" in evidence["identity_evidence"]
    assert evidence["source_fields"] == ["description", "requirement"]
    assert evidence["content_sha256"] == sha256(expected.encode("utf-8")).hexdigest()
    assert evidence["raw_response_sha256"] == sha256(
        _response_text(row).encode("utf-8")
    ).hexdigest()
    assert assess_jd_capture(job).complete
    stored = fetch_full_job_description_result(job)
    assert stored.status == "complete"
    assert stored.source == "stored"
    assert stored.detail == expected


def test_explicitly_short_official_fields_can_be_complete() -> None:
    row = _row(description="D", requirement="")
    job = _crawler()._parse_api_job(row, response_context=_context(row))

    assert job["jd_raw"] == "职位描述\nD"
    assert job["capture_evidence"]["status"] == "complete"
    assert assess_jd_capture(job).complete


def test_receipt_preserves_response_capture_time_when_reparsed() -> None:
    row = _row()
    before = datetime.now(timezone.utc)
    context = _context(row)
    assert context is not None
    captured = datetime.fromisoformat(context.captured_at)
    assert before <= captured <= datetime.now(timezone.utc)

    original_time = "2026-09-01T00:00:00+00:00"
    context = replace(context, captured_at=original_time)
    job = _crawler()._parse_api_job(row, response_context=context)
    assert job["capture_evidence"]["captured_at"] == original_time
    result = fetch_full_job_description_result(job)
    assert result.capture_evidence["captured_at"] == original_time


def test_receipt_binds_html_reconstruction_and_rejects_truncation_or_tampering() -> None:
    row = _row(
        description="<p>负责服务开发</p><p>推进稳定性建设 &amp; 复盘</p>",
        requirement="<ul><li>熟悉 Python</li><li>能独立排查问题</li></ul>",
    )
    context = _context(row)
    job = _crawler()._parse_api_job(row, response_context=context)
    expected = (
        "职位描述\n负责服务开发\n推进稳定性建设 & 复盘\n"
        "任职要求\n熟悉 Python\n能独立排查问题"
    )

    assert job["jd_raw"] == expected
    assert job["capture_evidence"]["status"] == "complete"
    for detail_text in (expected[:-1], expected.replace("稳定性", "可用性", 1)):
        assert (
            build_official_api_capture_evidence(
                context=context,
                item=row,
                detail_text=detail_text,
                detail_url=job["detail_url"],
                native_post_id=row["id"],
                title=row["title"],
            )
            == {}
        )


@pytest.mark.parametrize(
    ("row", "context"),
    [
        (_row(requirement=None), "same"),
        (_row(description="only description", requirement=None), "same"),
        (_row(description=None, requirement="only requirement"), "same"),
        (_row(summary="navigation summary", description=None, requirement=None), "same"),
        (
            _row(truncated=True),
            "truncated",
        ),
        (
            _row(post_id="9876543210987654321"),
            "id_mismatch",
        ),
        (
            _row(title="Different title"),
            "title_mismatch",
        ),
    ],
)
def test_missing_truncated_or_mismatched_rows_never_get_complete_receipt(
    row: dict, context: str
) -> None:
    if context == "same":
        response_context = _context(row)
    elif context == "truncated":
        response_context = _context(row)
        assert response_context is not None
    elif context == "id_mismatch":
        original = _row(post_id="1234567890123456789")
        response_context = _context(original)
    else:
        original = _row(title="Expected title")
        response_context = _context(original)

    job = _crawler()._parse_api_job(row, response_context=response_context)

    assert job["capture_evidence"] == {}
    assert not assess_jd_capture(job).complete


def test_plain_mapping_or_historical_row_cannot_authenticate_old_jd() -> None:
    row = _row()
    job = _crawler()._parse_api_job(row, response_context={"status": 200})

    assert job["jd_raw"]
    assert job["capture_evidence"] == {}
    assert not assess_jd_capture(job).complete


@pytest.mark.parametrize("value", [1.5, True, -1, "abc", "1e3", "１２３"])
def test_native_post_id_rejects_lossy_or_non_decimal_values(value: object) -> None:
    assert native_post_id(value) == ""


def test_api_context_rejects_userinfo_non_default_port_and_non_numeric_detail_route() -> None:
    row = _row()
    raw = _response_text(row)
    for url in (
        "https://user:pass@jobs.bytedance.com/api/v1/search/job/posts",
        "https://jobs.bytedance.com:8443/api/v1/search/job/posts",
    ):
        assert _context(row, response_text=raw, url=url) is None

    context = _context(row)
    detail_text = normalize_official_jd(row["description"], row["requirement"])
    for detail_url in (
        "https://jobs.bytedance.com/campus/position/not-numeric/detail",
        "https://user:pass@jobs.bytedance.com/campus/position/1234567890123456789/detail",
        "https://jobs.bytedance.com:8443/campus/position/1234567890123456789/detail",
    ):
        assert (
            build_official_api_capture_evidence(
                context=context,
                item=row,
                detail_text=detail_text,
                detail_url=detail_url,
                native_post_id=row["id"],
                title=row["title"],
            )
            == {}
        )


class _FakeLocator:
    @property
    def first(self):
        return self

    def wait_for(self, **_kwargs) -> None:
        return None

    def evaluate(self, _script) -> None:
        return None


class _FakeRequest:
    method = "POST"
    headers = {"x-csrf-token": "csrf"}

    def __init__(self, payload: dict):
        self.post_data_json = payload


class _FakeResponse:
    def __init__(self, url: str):
        self.url = url
        self.status = 200
        self.request = _FakeRequest(dict(REQUEST))


class _FakePage:
    def __init__(self, first_page: str):
        self._response_handlers = []
        self._first_page = first_page

    def on(self, event: str, callback) -> None:
        if event == "response":
            self._response_handlers.append(callback)

    def goto(self, *_args, **_kwargs) -> None:
        response = _FakeResponse(API_URL)
        for callback in self._response_handlers:
            callback(response)

    def wait_for_selector(self, *_args, **_kwargs) -> None:
        return None

    def get_by_text(self, *_args, **_kwargs):
        return _FakeLocator()

    def wait_for_timeout(self, *_args, **_kwargs) -> None:
        return None

    def evaluate(self, _script, args):
        if args["payload"]["offset"] == 0:
            return {"status": 200, "text": self._first_page}
        return {"status": 503, "text": ""}


class _FakeBrowserContext:
    def __init__(self, page: _FakePage):
        self._page = page

    def new_page(self):
        return self._page

    def close(self) -> None:
        return None


class _FakeBrowser:
    def __init__(self, page: _FakePage):
        self._page = page

    def new_context(self, **_kwargs):
        return _FakeBrowserContext(self._page)

    def close(self) -> None:
        return None


class _FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, *_args) -> None:
        return None


def test_incomplete_pagination_keeps_row_receipt_but_not_company_complete(monkeypatch) -> None:
    row = _row()
    page = _FakePage(_response_text(row, total=2))
    browser = _FakeBrowser(page)

    import playwright.sync_api as sync_api

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(bytedance_module, "launch_browser", lambda *_args, **_kwargs: browser)

    crawler = _crawler()
    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert jobs[0]["capture_evidence"]["status"] == "complete"
    assert crawler.pagination_complete is False
    assert crawler.has_more is True
    assert crawler.pages_seen == 1
    assert crawler.total_count == 2
    assert crawler.pagination_termination_reason == "api_request_failed"
