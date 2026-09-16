from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

import pytest

import packages.recruitment_core.crawlers.hotjob as hotjob_module
from packages.recruitment_core.crawlers.hotjob import (
    HotjobRecruitCrawler,
    _fetch_hotjob_position_detail,
    fetch_hotjob_position_detail,
)
from packages.recruitment_core.jd_capture import assess_jd_capture


SUITE = "SU63eed9fd2f9d246c468eb43d"
ORIGIN = f"https://wecruit.hotjob.cn/{SUITE}"
DETAIL_URL = f"{ORIGIN}/pb/posDetail.html?postId=p1&postType=campus"
DETAIL_API = f"https://wecruit.hotjob.cn/wecruit/positionInfo/listPositionDetail/{SUITE}"


class _Response:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        url: str = DETAIL_API,
        history: list[Any] | None = None,
    ) -> None:
        self.payload = payload
        self.url = url
        self.history = history or []

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def _payload(
    *,
    post_id: str | None = "p1",
    title: str | None = "软件开发工程师",
    duties: str | None = "负责软件开发。",
    requirements: str | None = "熟悉 Python。",
    extra: str | None = "接受应届毕业生。",
    **fields: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if post_id is not None:
        data["postId"] = post_id
    if title is not None:
        data["postName"] = title
    if duties is not None:
        data["workContent"] = duties
    if requirements is not None:
        data["serviceCondition"] = requirements
    if extra is not None:
        data["applyPositionContent"] = extra
    data.update(fields)
    return {"state": "200", "data": data}


def _job() -> dict[str, str]:
    return {
        "title": "软件开发工程师",
        "jd_url": DETAIL_URL,
        "jd_raw": "列表原文摘要",
        "source_job_id": "p1",
        "source_post_id": "p1",
    }


def _hydrate(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    *,
    response_url: str = DETAIL_API,
    history: list[Any] | None = None,
) -> tuple[HotjobRecruitCrawler, list[dict[str, Any]], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> _Response:
        calls.append({"url": url, **kwargs})
        return _Response(payload, url=response_url, history=history)

    monkeypatch.setattr(hotjob_module.requests, "post", fake_post)
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/pb/school.html")
    crawler.DETAIL_WORKERS = 1
    jobs = crawler._hydrate_api_jobs([_job()])
    return crawler, jobs, calls


def test_official_capture_keeps_full_long_content_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duties = "职责段落。" * 2200
    requirements = "要求段落。" * 1800
    extra = "补充说明。" * 300
    before = datetime.now(timezone.utc)
    crawler, jobs, calls = _hydrate(
        monkeypatch,
        _payload(duties=duties, requirements=requirements, extra=extra),
    )

    expected = f"职位描述\n{duties}\n任职要求\n{requirements}\n补充说明\n{extra}"
    job = jobs[0]
    evidence = job["capture_evidence"]
    assert len(job["jd_raw"]) > 12000
    assert job["jd_raw"] == expected
    assert evidence["status"] == "complete"
    assert before <= datetime.fromisoformat(evidence["captured_at"]) <= datetime.now(timezone.utc)
    assert evidence["identity_verified"] is True
    assert evidence["terminal_observed"] is True
    assert evidence["content_sha256"] == sha256(expected.encode("utf-8")).hexdigest()
    assert evidence["request_identity"]["post_id"] == "p1"
    assert evidence["response_identity"]["post_id"] == "p1"
    assert len(evidence["receipt_sha256"]) == 64
    assert assess_jd_capture(job).complete
    assert len(calls) == 1
    assert calls[0]["url"] == DETAIL_API
    assert calls[0]["data"] == {"postId": "p1"}
    assert crawler.detail_complete is True


def test_short_official_content_can_be_complete_without_length_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _crawler, jobs, calls = _hydrate(
        monkeypatch,
        _payload(duties="负责核心模块。", requirements="掌握基础编程。", extra=None),
    )

    assert jobs[0]["jd_raw"] == "职位描述\n负责核心模块。\n任职要求\n掌握基础编程。"
    assert jobs[0]["capture_evidence"]["status"] == "complete"
    assert assess_jd_capture(jobs[0]).complete
    assert len(calls) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "http://wecruit.hotjob.cn",
        "https://user@wecruit.hotjob.cn",
        "https://wecruit.hotjob.cn:8443",
        "https://wecruit.hotjob.cn:invalid",
    ],
)
def test_capture_rejects_uncontrolled_request_authority(monkeypatch, origin):
    calls = []
    monkeypatch.setattr(
        hotjob_module.requests, "post", lambda *args, **kwargs: calls.append(args)
    )
    result = _fetch_hotjob_position_detail(
        f"{origin}/{SUITE}/pb/posDetail.html?postId=p1",
        capture_evidence=True,
        expected_post_id="p1",
        expected_title="软件开发工程师",
    )
    assert calls == []
    assert result[2] == "error"
    assert "request_host_uncontrolled" in result[3]["failure_reasons"]


@pytest.mark.parametrize(
    "response_url",
    [
        DETAIL_API.replace("https:", "http:"),
        DETAIL_API.replace("https://", "https://user@"),
        DETAIL_API.replace("hotjob.cn/", "hotjob.cn:8443/"),
        f"{ORIGIN}/pb/login.html",
    ],
)
def test_capture_rejects_response_authority_or_api_route(monkeypatch, response_url):
    _crawler, jobs, _calls = _hydrate(
        monkeypatch, _payload(), response_url=response_url
    )
    assert jobs[0]["capture_evidence"]["status"] == "incomplete"
    assert not assess_jd_capture(jobs[0]).complete


def test_explicitly_empty_requirements_are_valid_official_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _crawler, jobs, calls = _hydrate(
        monkeypatch,
        _payload(duties="职责很短。", requirements="", extra=None),
    )

    assert jobs[0]["jd_raw"] == "职位描述\n职责很短。"
    assert jobs[0]["capture_evidence"]["status"] == "complete"
    assert assess_jd_capture(jobs[0]).complete
    assert len(calls) == 1


def test_list_hydration_attaches_receipt_from_the_single_detail_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/pb/school.html")
    crawler.DETAIL_WORKERS = 1
    calls: list[dict[str, Any]] = []
    list_api = crawler._api_url()

    def fake_post(url: str, **kwargs: Any) -> _Response:
        calls.append({"url": url, **kwargs})
        if url == list_api:
            return _Response(
                {
                    "state": "200",
                    "data": {
                        "pageForm": {
                            "totalPage": 1,
                            "pageSize": 1,
                            "dataCount": 1,
                            "pageData": [
                                {
                                    "postId": "p1",
                                    "postName": "软件开发工程师",
                                    "workPlaceStr": "北京市",
                                }
                            ],
                        }
                    },
                },
                url=list_api,
            )
        return _Response(_payload(), url=DETAIL_API)

    monkeypatch.setattr(hotjob_module.requests, "post", fake_post)
    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert jobs[0]["capture_evidence"]["status"] == "complete"
    assert [call["url"] for call in calls] == [list_api, DETAIL_API]
    assert sum(call["url"] == DETAIL_API for call in calls) == 1


@pytest.mark.parametrize(
    ("message", "expected_status", "failure"),
    [
        ("岗位已关闭", "closed", "response_closed"),
        ("服务暂不可用", "error", "response_error"),
    ],
)
def test_error_and_closed_preserve_list_text_without_certifying(
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected_status: str,
    failure: str,
) -> None:
    crawler, jobs, calls = _hydrate(
        monkeypatch,
        {"state": "500", "msg": message},
    )

    assert len(jobs) == 1
    assert jobs[0]["jd_raw"] == "列表原文摘要"
    assert jobs[0]["capture_evidence"]["status"] == "incomplete"
    assert jobs[0]["capture_evidence"]["response_status"] == expected_status
    assert failure in jobs[0]["capture_evidence"]["failure_reasons"]
    assert jobs[0]["capture_evidence"]["terminal_observed"] is False
    assert crawler.detail_complete is False
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("response_url", "failure"),
    [
        ("https://evil.example/SU63eed9fd2f9d246c468eb43d/detail", "response_host_uncontrolled"),
        (
            "https://other.hotjob.cn/SU63eed9fd2f9d246c468eb43d/detail",
            "response_host_mismatch",
        ),
        (
            "https://wecruit.hotjob.cn/SUabcdef1234567890abcdef12/detail",
            "response_tenant_mismatch",
        ),
    ],
)
def test_redirect_host_or_tenant_is_not_certified(
    monkeypatch: pytest.MonkeyPatch,
    response_url: str,
    failure: str,
) -> None:
    _crawler, jobs, calls = _hydrate(
        monkeypatch,
        _payload(),
        response_url=response_url,
    )

    evidence = jobs[0]["capture_evidence"]
    assert evidence["status"] == "incomplete"
    assert evidence["identity_verified"] is False
    assert failure in evidence["failure_reasons"]
    assert len(calls) == 1


def test_identity_mismatch_preserves_response_text_but_not_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _crawler, jobs, calls = _hydrate(
        monkeypatch,
        _payload(post_id="other-post", title="另一个岗位"),
    )

    evidence = jobs[0]["capture_evidence"]
    assert jobs[0]["jd_raw"].startswith("职位描述\n负责软件开发")
    assert evidence["status"] == "incomplete"
    assert evidence["identity_verified"] is False
    assert "response_post_id_mismatch" in evidence["failure_reasons"]
    assert "response_title_mismatch" in evidence["failure_reasons"]
    assert len(calls) == 1


def test_missing_identity_or_content_fields_cannot_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _crawler, identity_jobs, identity_calls = _hydrate(
        monkeypatch,
        _payload(post_id=None, title=None),
    )
    identity_evidence = identity_jobs[0]["capture_evidence"]
    assert identity_evidence["status"] == "incomplete"
    assert identity_evidence["identity_verified"] is False
    assert "response_post_id_missing" in identity_evidence["failure_reasons"]
    assert len(identity_calls) == 1

    _crawler, content_jobs, content_calls = _hydrate(
        monkeypatch,
        _payload(requirements=None),
    )
    content_evidence = content_jobs[0]["capture_evidence"]
    assert content_jobs[0]["jd_raw"].startswith("职位描述\n负责软件开发")
    assert content_evidence["status"] == "incomplete"
    assert "requirements_field_missing" in content_evidence["failure_reasons"]
    assert len(content_calls) == 1


@pytest.mark.parametrize(
    ("fields", "failure"),
    [
        ({"title": "另一个岗位"}, "response_title_conflict"),
        ({"postID": "p2"}, "response_post_id_conflict"),
    ],
)
def test_conflicting_same_namespace_identity_fields_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    fields: dict[str, str],
    failure: str,
) -> None:
    payload = _payload()
    payload["data"].update(fields)
    _crawler, jobs, calls = _hydrate(monkeypatch, payload)

    evidence = jobs[0]["capture_evidence"]
    assert evidence["status"] == "incomplete"
    assert evidence["identity_verified"] is False
    assert failure in evidence["failure_reasons"]
    assert len(calls) == 1


def test_internal_id_does_not_conflict_with_post_id_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _crawler, jobs, calls = _hydrate(
        monkeypatch,
        _payload(id="internal-42"),
    )

    assert jobs[0]["capture_evidence"]["status"] == "complete"
    assert jobs[0]["capture_evidence"]["response_identity"]["post_id"] == "p1"
    assert jobs[0]["capture_evidence"]["response_identity"]["id_namespace"] == "post_id"
    assert len(calls) == 1


def test_default_detail_helpers_keep_legacy_tuple_and_truncation_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duties = "职责。" * 3000
    requirements = "要求。" * 3000
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> _Response:
        calls.append({"url": url, **kwargs})
        return _Response(_payload(duties=duties, requirements=requirements))

    monkeypatch.setattr(hotjob_module.requests, "post", fake_post)
    legacy = _fetch_hotjob_position_detail(DETAIL_URL)
    wrapped = fetch_hotjob_position_detail(DETAIL_URL)

    assert len(legacy) == 3
    assert legacy[2] == "active"
    assert len(legacy[0]) == 12000
    assert len(wrapped) == 2
    assert wrapped[0] == legacy[0]
    assert wrapped[1] == legacy[1]
    assert len(calls) == 2


def test_legacy_string_tuple_double_is_not_signed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_detail(url: str) -> tuple[str, str, str]:
        return "职位描述\n旧 mock 正文", url, "active"

    monkeypatch.setattr(hotjob_module, "_fetch_hotjob_position_detail", fake_detail)
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/pb/school.html")
    crawler.DETAIL_WORKERS = 1
    jobs = crawler._hydrate_api_jobs([_job()])

    assert jobs[0]["jd_raw"] == "职位描述\n旧 mock 正文"
    assert jobs[0].get("capture_evidence", {}).get("status") != "complete"
    assert "capture_evidence" not in jobs[0]
    assert crawler.detail_complete is True
