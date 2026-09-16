from __future__ import annotations

from hashlib import sha256
from typing import Any

import pytest
import requests

from packages.recruitment_core.jd_detail_adapter import (
    JdDetailResult,
    clean_jd_body,
    fetch_jd_detail,
    jd_detail_api_url,
)


DETAIL_URL = "https://campus.jd.com/#/details?type=present&id=9086"
API_URL = "https://campus.jd.com/api/wx/position/detail/9086"


class Response:
    def __init__(self, payload: object, *, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.url = API_URL

    def json(self) -> object:
        return self._payload


def _payload(
    *,
    publish_id: object = 9086,
    req_id: object = 2383,
    title: object = "机器人软件工程师",
    work_content: object = "<p>负责核心模块。<br>推进交付。</p>",
    qualification: object = "掌握 Python。",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "publishId": publish_id,
        "reqId": req_id,
        "positionName": title,
        "workContent": work_content,
        "qualification": qualification,
    }
    return {"success": True, "body": body}


def test_spa_route_is_translated_to_official_api() -> None:
    assert jd_detail_api_url(DETAIL_URL) == API_URL
    assert jd_detail_api_url(API_URL) == API_URL


def test_success_binds_publish_id_and_title_and_keeps_short_cleaned_jd() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def opener(url: str, **kwargs: Any) -> Response:
        calls.append((url, kwargs))
        return Response(_payload())

    result = fetch_jd_detail(
        DETAIL_URL,
        expected_publish_id=9086,
        expected_title="机器人软件工程师",
        opener=opener,
    )

    assert result == JdDetailResult(
        "complete",
        detail_url=DETAIL_URL,
        request_url=API_URL,
        publish_id="9086",
        title="机器人软件工程师",
        detail="岗位职责\n负责核心模块。\n推进交付。\n任职要求\n掌握 Python。",
        req_id="2383",
        response_status=200,
        observations=result.observations,
        capture_evidence=result.capture_evidence,
    )
    assert result.complete
    assert result.capture_evidence["status"] == "complete"
    assert result.capture_evidence["identity_verified"] is True
    assert result.capture_evidence["native_job_id"] == "9086"
    assert result.capture_evidence["req_id"] == "2383"
    assert result.observations["identity"]["publish_id_match"] is True
    assert result.observations["identity"]["title_match"] is True
    assert len(result.detail) < 100
    assert calls == [(API_URL, {"headers": {
        "Accept": "application/json",
        "Referer": "https://campus.jd.com/",
        "User-Agent": "Mozilla/5.0",
    }, "timeout": 20.0})]


def test_clean_body_omits_empty_fields_and_does_not_require_length() -> None:
    assert clean_jd_body({"workContent": "<div>短 JD</div>", "qualification": ""}) == "岗位职责\n短 JD"
    assert clean_jd_body({"workContent": "<script>discard()</script>职责"}) == "岗位职责\n职责"


def test_req_id_is_never_promoted_when_publish_id_is_missing() -> None:
    result = fetch_jd_detail(
        API_URL,
        expected_title="机器人软件工程师",
        opener=lambda *_args, **_kwargs: Response(
            _payload(publish_id=None, req_id="req-2383")
        ),
    )

    assert result.status == "identity_unobserved"
    assert result.publish_id == ""
    assert result.req_id == "req-2383"
    assert result.capture_evidence["identity_verified"] is False
    assert "publish_id_missing" in result.observations["failure_reasons"]


@pytest.mark.parametrize(
    ("payload_kwargs", "expected_status"),
    [
        ({"publish_id": 9210}, "publish_id_mismatch"),
        ({"title": "另一个岗位"}, "title_mismatch"),
    ],
)
def test_identity_conflicts_are_not_certified(
    payload_kwargs: dict[str, Any], expected_status: str
) -> None:
    result = fetch_jd_detail(
        API_URL,
        expected_publish_id="9086",
        expected_title="机器人软件工程师",
        opener=lambda *_args, **_kwargs: Response(_payload(**payload_kwargs)),
    )

    assert result.status == expected_status
    assert result.detail
    assert result.capture_evidence["status"] == "incomplete"
    assert result.capture_evidence["identity_verified"] is False


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (Response({"success": False, "message": "请先登录"}), "login_required"),
        (Response({"success": False, "message": "岗位不存在"}), "not_found"),
        (Response({}, status_code=404), "not_found"),
    ],
)
def test_login_and_not_found_are_distinct(response: Response, expected_status: str) -> None:
    result = fetch_jd_detail(API_URL, opener=lambda *_args, **_kwargs: response)
    assert result.status == expected_status
    assert result.capture_evidence["response_status"] == response.status_code
    assert result.capture_evidence["status"] == "incomplete"


def test_timeout_is_distinct_and_non_throwing() -> None:
    def opener(*_args: Any, **_kwargs: Any) -> Response:
        raise requests.Timeout("deadline exceeded")

    result = fetch_jd_detail(API_URL, opener=opener)
    assert result.status == "timeout"
    assert result.error_type == "Timeout"
    assert result.detail == ""


def test_observation_is_structured_and_content_hash_is_bound() -> None:
    result = fetch_jd_detail(
        API_URL,
        expected_publish_id="9086",
        expected_title="机器人软件工程师",
        opener=lambda *_args, **_kwargs: Response(_payload()),
    )
    content_hash = sha256(result.detail.encode("utf-8")).hexdigest()
    assert result.observations["schema"] == "jd_official_detail_observation.v1"
    assert result.observations["request"]["route_publish_id"] == "9086"
    assert result.observations["response"]["body_keys"] == [
        "positionName", "publishId", "qualification", "reqId", "workContent"
    ]
    assert result.observations["content"]["sha256"] == content_hash
    assert result.capture_evidence["content_sha256"] == content_hash
    assert result.as_hydration_mapping()["publish_id"] == "9086"


def test_clean_jd_body_preserves_escaped_code_literals() -> None:
    detail = clean_jd_body(
        {"workContent": "<p>熟悉 C++ &lt;vector&gt; 与 &lt;T&gt;。</p>"}
    )
    assert "&lt;vector&gt;" not in detail
    assert "<vector>" in detail
    assert "<T>" in detail
