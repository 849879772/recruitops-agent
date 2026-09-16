from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from hashlib import sha256

import pytest
import requests

from packages.matching.rules import is_jd_incomplete as matching_jd_incomplete
from packages.recruitment_core import job_details


MOKA_JOB_ID = "78086983-48dd-4914-bbc1-90302918825b"
MOKA_IV = "fedcba9876543210"
MOKA_SITE_HTML = (
    '<input id="init-data" value="{&quot;orgId&quot;:&quot;demo&quot;,'
    '&quot;siteId&quot;:54046,&quot;aesIv&quot;:&quot;fedcba9876543210&quot;}">'
)
UNTITLED_COMPLETE_JD = (
    "1. 负责机器人控制软件的模块设计、核心代码开发与持续维护，跟进线上问题并推动稳定性优化；"
    "2. 参与传感器、执行器和上层规划模块的接口设计，完成联调测试、性能分析及故障定位；"
    "3. 构建自动化测试与发布流程，沉淀可复用工具，支持产品在不同硬件平台上的部署；"
    "4. 与算法、硬件和产品团队协作，评审技术方案，推进关键功能按计划交付并持续改进；"
    "5. 熟悉 C++、Python、Linux 和常用数据结构，具备良好的编码规范与工程实践能力；"
    "6. 能够阅读英文技术资料，掌握多线程或网络编程，有机器人项目经验者优先，并具备清晰沟通能力。"
)


def _capture_evidence(
    detail: str,
    *,
    source_url: str = "https://example.test/job/101",
    method: str = "fixture_api",
) -> dict:
    normalized = detail.strip()
    return {
        "status": "complete",
        "method": method,
        "source_url": source_url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(normalized.encode("utf-8")).hexdigest(),
    }


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("JD regression tests must not make live requests")

    monkeypatch.setattr(job_details.requests, "get", unexpected)
    monkeypatch.setattr(job_details.requests, "post", unexpected)
    monkeypatch.setattr(job_details, "render_page", unexpected)


class Response:
    def __init__(self, *, text: str = "", payload: object = None, url: str = ""):
        self.text = text
        self._payload = payload
        self.url = url
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


@pytest.mark.parametrize(
    "checker",
    [job_details.is_jd_incomplete, matching_jd_incomplete],
)
def test_untitled_structured_250_to_300_character_jd_is_complete(checker) -> None:
    assert 250 <= len(UNTITLED_COMPLETE_JD) <= 300
    assert checker({
        "title": "机器人软件工程师",
        "jd_raw": UNTITLED_COMPLETE_JD,
        "capture_evidence": _capture_evidence(UNTITLED_COMPLETE_JD),
    }) is False


@pytest.mark.parametrize(
    "checker",
    [job_details.is_jd_incomplete, matching_jd_incomplete],
)
def test_title_date_and_location_list_shell_stays_incomplete(checker) -> None:
    shell = "机器人软件工程师 发布于 2026-09-01 上海 校园招聘 全职 研发类 申请 收藏 分享"
    assert checker({"title": "机器人软件工程师", "jd_raw": shell}) is True


def test_hydration_result_distinguishes_non_fetchable_urls(monkeypatch) -> None:
    assert job_details.fetch_full_job_description_result({"jd_raw": ""}).status == "no_detail_url"

    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(job_details, "_configured_careers_urls", lambda: {})
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: "<main></main>")
    result = job_details.fetch_full_job_description_result(
        {
            "title": "机器人软件工程师",
            "jd_raw": "",
            "jd_url": "https://example.test/jobs",
            "link_kind": "list",
        }
    )
    assert result.status == "list_url"
    assert result.detail == ""
    assert result.attempts == ("configured_page:list_url", "configured_page_render:list_url")


def test_configured_list_renders_required_scope_before_matching_job(monkeypatch) -> None:
    static_social = "<main><h2>加入灵心巧手</h2><h3>机器人产品经理</h3></main>"
    rendered_campus = """
    <html data-recruitops-capture-status="complete"
          data-recruitops-capture-method="detail_interaction:inline"
          data-recruitops-terminal-observed="true"
          data-recruitops-remaining-controls="[]">
      <main>
        <h2>加入灵心巧手</h2>
        <div class="job-card">
          <h3 class="job-title">具身 AI Infra 研发工程师</h3>
          <div class="job-description">
            <h4>职位职责</h4>
            <p>负责模型训练、数据处理、实验管理、模型管理、评测调度和推理服务。</p>
            <h4>任职要求</h4>
            <p>熟悉 Python、Linux、容器和分布式系统，具备良好的工程实践能力。</p>
          </div>
        </div>
      </main>
    </html>
    """
    calls = {}
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(text=static_social, url="https://www.linkerbot.cn/about/join/"),
    )

    def render(*_args, **kwargs):
        calls.update(kwargs)
        return rendered_campus

    monkeypatch.setattr(job_details, "render_page", render)
    result = job_details.fetch_configured_page_job_description_result({
        "title": "具身 AI Infra 研发工程师",
        "careers_url": "https://www.linkerbot.cn/about/join/",
        "link_kind": "list",
        "entry_click_texts": ["校园招聘"],
        "detail_interaction": {
            "mode": "inline",
            "trigger_selector": ".job-card",
            "container_selector": ".job-card",
            "bind_job_id": False,
        },
    })

    assert result.complete
    assert result.identity_evidence == ("title:具身 AI Infra 研发工程师",)
    assert calls["click_texts"] == ["校园招聘"]
    assert calls["detail_title"] == "具身 AI Infra 研发工程师"
    assert calls["detail_job_id"] == ""
    assert result.attempts == (
        "configured_page:identity_ambiguous",
        "configured_page_render:complete",
    )


def test_configured_list_does_not_accept_social_job_before_required_scope_click(monkeypatch) -> None:
    static_social = """
    <main><div class="job-card"><h3>机器人产品经理</h3>
    <h4>职位职责</h4><p>负责社招产品规划和项目管理。</p></div></main>
    """
    rendered_campus = """
    <main><h2>加入灵心巧手</h2>
    <div class="job-card"><h3>具身 AI Infra 研发工程师</h3></div>
    <div class="job-card"><h3>具身强化学习算法工程师</h3></div></main>
    """
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(text=static_social, url="https://www.linkerbot.cn/about/join/"),
    )
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: rendered_campus)

    result = job_details.fetch_configured_page_job_description_result({
        "title": "机器人产品经理",
        "careers_url": "https://www.linkerbot.cn/about/join/",
        "link_kind": "list",
        "entry_click_texts": ["校园招聘"],
    })

    assert not result.complete
    assert result.status in {"identity_mismatch", "identity_ambiguous"}


def test_hydration_result_reports_render_failure_incomplete_content_and_timeout(
    monkeypatch,
) -> None:
    job = {
        "title": "机器人软件工程师",
        "jd_raw": "",
        "jd_url": "https://example.test/job/1",
    }
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: None)
    assert job_details.fetch_full_job_description_result(job).status == "render_failed"

    monkeypatch.setattr(
        job_details,
        "render_page",
        lambda *_args, **_kwargs: "<main><h2>岗位职责</h2><p>负责开发。</p></main>",
    )
    incomplete = job_details.fetch_full_job_description_result(job)
    assert incomplete.status == "content_incomplete"
    assert incomplete.detail == ""

    def timeout(*_args, **_kwargs):
        raise requests.Timeout("fixture timeout")

    monkeypatch.setattr(job_details, "render_page", timeout)
    timed_out = job_details.fetch_full_job_description_result(job)
    assert timed_out.status == "timeout"
    assert timed_out.error_type == "Timeout"


def test_legacy_string_api_remains_compatible_and_exceptions_do_not_escape(monkeypatch) -> None:
    job = {
        "title": "机器人软件工程师",
        "jd_raw": UNTITLED_COMPLETE_JD,
        "jd_url": "https://example.test/job/1",
        "capture_evidence": _capture_evidence(UNTITLED_COMPLETE_JD, source_url="https://example.test/job/1"),
    }
    assert job_details.fetch_full_job_description(job) == UNTITLED_COMPLETE_JD

    monkeypatch.setattr(
        job_details,
        "render_page",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )
    failed = job_details.fetch_full_job_description_result({**job, "jd_raw": ""})
    assert failed.status == "fetch_failed"
    assert failed.error_type == "RuntimeError"
    assert failed.error_detail == "fixture failure"


def test_moka_accepts_clear_detail_api_variant(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(text=MOKA_SITE_HTML),
    )
    monkeypatch.setattr(
        job_details.requests,
        "post",
        lambda *_args, **_kwargs: Response(
            payload={
                "code": 0,
                "data": {
                    "id": MOKA_JOB_ID,
                    "title": "机器人软件工程师",
                    "jobDescription": UNTITLED_COMPLETE_JD,
                },
            }
        ),
    )

    detail, status = job_details.fetch_moka_job_description_status(
        f"https://app.mokahr.com/campus-recruitment/demo/54046#/job/{MOKA_JOB_ID}"
    )
    assert status == "complete"
    assert detail == UNTITLED_COMPLETE_JD


def test_moka_unsupported_detail_variant_falls_back_to_official_list(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()
    calls: list[str] = []
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(text=MOKA_SITE_HTML),
    )

    def post(url: str, **_kwargs) -> Response:
        calls.append(url)
        if url.endswith("/website/job"):
            return Response(payload={"unexpected": "new-envelope"})
        return Response(
            payload={
                "code": 0,
                "data": {
                    "jobs": [
                        {
                            "id": MOKA_JOB_ID,
                            "title": "机器人软件工程师",
                            "jobDescription": UNTITLED_COMPLETE_JD,
                        }
                    ]
                },
            }
        )

    monkeypatch.setattr(job_details.requests, "post", post)
    detail, status = job_details.fetch_moka_job_description_status(
        f"https://app.mokahr.com/campus-recruitment/demo/54046#/job/{MOKA_JOB_ID}"
    )

    assert status == "complete"
    assert detail == UNTITLED_COMPLETE_JD
    assert calls == [
        "https://app.mokahr.com/api/outer/ats-apply/website/job",
        "https://app.mokahr.com/api/outer/ats-apply/website/jobs/v2",
    ]


def test_moka_reports_unsupported_status_when_both_api_shapes_are_unknown(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(text=MOKA_SITE_HTML),
    )
    monkeypatch.setattr(
        job_details.requests,
        "post",
        lambda *_args, **_kwargs: Response(payload={"unexpected": "new-envelope"}),
    )

    detail, status = job_details.fetch_moka_job_description_status(
        f"https://app.mokahr.com/campus-recruitment/demo/54046#/job/{MOKA_JOB_ID}"
    )
    assert detail == ""
    assert status == "api_variant_unsupported"

    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: None)
    result = job_details.fetch_full_job_description_result(
        {
            "title": "机器人软件工程师",
            "jd_raw": "",
            "jd_url": (
                "https://app.mokahr.com/campus-recruitment/demo/54046"
                f"#/job/{MOKA_JOB_ID}"
            ),
        }
    )
    assert result.status == "render_failed"
    assert "moka_detail_api:api_variant_unsupported" in result.attempts
    assert result.attempts[-1] == "render:render_failed"


def test_moka_path_job_can_fall_back_to_validated_direct_html(monkeypatch) -> None:
    body = html.escape(UNTITLED_COMPLETE_JD)
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(
            text=f'<main><h1>机器人软件工程师</h1>{body}</main>'
        ),
    )

    detail, status = job_details.fetch_moka_job_description_status(
        f"https://app.mokahr.com/job/{MOKA_JOB_ID}",
        title="机器人软件工程师",
    )
    assert status == "complete"
    assert "负责机器人控制软件" in detail


def test_moka_timeout_is_diagnostic_and_does_not_escape(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()

    def timeout(*_args, **_kwargs):
        raise requests.Timeout("fixture timeout")

    monkeypatch.setattr(job_details.requests, "get", timeout)
    detail, status = job_details.fetch_moka_job_description_status(
        f"https://app.mokahr.com/campus-recruitment/demo/54046#/job/{MOKA_JOB_ID}"
    )
    assert detail == ""
    assert status == "timeout"


def _job(**overrides) -> dict:
    return {
        "title": "C++工程师",
        "company": "Example",
        "jd_raw": "",
        "jd_url": "https://example.test/job/101",
        **overrides,
    }


def _card(title: str, native_id: str, body: str = UNTITLED_COMPLETE_JD) -> str:
    return (
        f'<article data-job-id="{native_id}" data-company="Example">'
        f'<h2>{html.escape(title)}</h2><div>{html.escape(body)}</div></article>'
    )


def _captured_page(body: str, *, method: str = "fixture_dom") -> str:
    return (
        '<html data-recruitops-capture-status="complete" '
        f'data-recruitops-capture-method="{method}" '
        'data-recruitops-terminal-observed="true" '
        'data-recruitops-remaining-controls="[]">'
        f"{body}</html>"
    )


def test_nested_hidden_style_nodes_do_not_raise_attribute_error(monkeypatch) -> None:
    page = (
        "<main><h1>机器人软件工程师</h1>"
        '<div style="display: none"><span style="display: none">hidden</span></div>'
        "<h2>岗位职责</h2>"
        + UNTITLED_COMPLETE_JD
        + "</main>"
    )
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)

    result = job_details.fetch_full_job_description_result(_job(title="机器人软件工程师"))

    assert result.status == "complete"
    assert result.detail
    assert result.error_type == ""
    assert result.error_detail == ""


def test_fresh_complete_capture_receipt_gets_utc_captured_at(monkeypatch) -> None:
    page = _captured_page(
        "<main><h1>机器人软件工程师</h1><h2>岗位职责</h2>"
        + UNTITLED_COMPLETE_JD
        + "</main>",
    )
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)

    started_at = datetime.now(timezone.utc)
    result = job_details.fetch_full_job_description_result(
        _job(title="机器人软件工程师")
    )
    finished_at = datetime.now(timezone.utc)

    assert result.status == "complete"
    captured_at = result.capture_evidence["captured_at"]
    parsed = datetime.fromisoformat(captured_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert started_at <= parsed <= finished_at


@pytest.mark.parametrize("original_captured_at", [
    "2026-01-02T03:04:05+00:00",
    None,
])
def test_reused_complete_receipt_preserves_or_omits_captured_at(
    original_captured_at: str | None,
) -> None:
    evidence = _capture_evidence(UNTITLED_COMPLETE_JD)
    if original_captured_at is not None:
        evidence["captured_at"] = original_captured_at

    result = job_details.fetch_full_job_description_result(
        _job(jd_raw=UNTITLED_COMPLETE_JD, capture_evidence=evidence)
    )

    assert result.status == "complete"
    if original_captured_at is None:
        assert "captured_at" not in result.capture_evidence
    else:
        assert result.capture_evidence["captured_at"] == original_captured_at


def test_identity_mismatch_has_structured_bounded_diagnostic(monkeypatch) -> None:
    page = _card("会计", "102")
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)

    result = job_details.fetch_full_job_description_result(
        _job(native_job_id="101")
    )
    diagnostic = result.identity_diagnostic

    assert result.status == "identity_mismatch"
    assert result.detail == ""
    assert diagnostic["status"] == "identity_mismatch"
    assert diagnostic["requested_job_id"] == "101"
    assert diagnostic["requested_title"] == "C++工程师"
    assert "102" in diagnostic["observed_job_ids"]
    assert "会计" in diagnostic["observed_titles"]
    assert diagnostic["source"] == "render"
    assert diagnostic["url"] == _job()["jd_url"]
    assert diagnostic["failed_step"] == "render:identity_mismatch"
    assert diagnostic["requested_route_job_id"] == "101"
    assert diagnostic["request_bound_ids"] == ["101"]
    assert diagnostic["observation_status"] == "observed"
    assert diagnostic["exception"] == {"type": "", "detail": ""}
    assert result.capture_evidence["identity_diagnostic"] == diagnostic


def test_identity_diagnostic_separates_request_ids_and_sanitizes_url() -> None:
    detail_url = (
        "https://example.test/jobs/101?tenant=acme&jobId=101&"
        "access_token=token-secret&signature=signature-secret"
    )
    request_only = job_details._detail_result(
        _job(native_job_id=""),
        "",
        "identity_mismatch",
        source="render",
        detail_url=detail_url,
        attempts=("render:identity_mismatch",),
        identity_evidence=("request_id:101",),
    )
    request_diagnostic = request_only.identity_diagnostic
    assert request_diagnostic["requested_job_id"] == ""
    assert request_diagnostic["requested_route_job_id"] == "101"
    assert request_diagnostic["request_bound_ids"] == ["101"]
    assert request_diagnostic["observed_job_ids"] == []
    assert request_diagnostic["observation_status"] == "not_observed"
    assert "token-secret" not in request_diagnostic["url"]
    assert "signature-secret" not in request_diagnostic["url"]
    assert "access_token" not in request_diagnostic["url"]
    assert "signature" not in request_diagnostic["url"]
    assert "tenant=acme" in request_diagnostic["url"]

    beisen_route = job_details._detail_result(
        _job(native_job_id=""),
        "",
        "identity_mismatch",
        source="render",
        detail_url="https://611.zhiye.com/zpdetail/351602837",
        attempts=("render:identity_mismatch",),
        identity_evidence=(),
    )
    assert beisen_route.identity_diagnostic["requested_route_job_id"] == "351602837"
    assert beisen_route.identity_diagnostic["request_bound_ids"] == ["351602837"]
    assert beisen_route.identity_diagnostic["observed_job_ids"] == []
    assert beisen_route.identity_diagnostic["observation_status"] == "not_observed"

    native_observed = job_details._detail_result(
        _job(native_job_id=""),
        "",
        "identity_mismatch",
        source="render",
        detail_url=detail_url,
        attempts=("render:identity_mismatch",),
        identity_evidence=("request_id:101", "native_id:102"),
    )
    assert native_observed.identity_diagnostic["observed_job_ids"] == ["102"]
    assert native_observed.identity_diagnostic["observation_status"] == "observed"


def test_identity_diagnostic_malformed_url_returns_safe_marker() -> None:
    result = job_details._detail_result(
        _job(native_job_id=""),
        "",
        "identity_mismatch",
        source="render",
        detail_url="https://[malformed",
        attempts=("render:identity_mismatch",),
        identity_evidence=("request_id:101",),
    )

    assert result.identity_diagnostic["url"] == "[malformed-url]"
    assert result.identity_diagnostic["requested_route_job_id"] == ""
    assert result.identity_diagnostic["observed_job_ids"] == []


def test_identity_diagnostic_removes_url_credentials_and_keeps_safe_context() -> None:
    result = job_details._detail_result(
        _job(native_job_id=""),
        "",
        "identity_mismatch",
        source="render",
        detail_url=(
            "https://alice:password@example.test:8443/jobs/101?jobId=101&"
            "tenant=acme&token=query-secret&auth=auth-secret"
        ),
        attempts=("render:identity_mismatch",),
        identity_evidence=("request_id:101",),
    )

    safe_url = result.identity_diagnostic["url"]
    assert safe_url == "https://example.test:8443/jobs/101?jobId=101&tenant=acme"
    assert "alice" not in safe_url
    assert "password" not in safe_url
    assert "query-secret" not in safe_url
    assert "auth-secret" not in safe_url


def test_identity_ambiguous_diagnostic_keeps_all_observed_ids(monkeypatch) -> None:
    page = _card("C++工程师", "101") + _card("C++工程师", "102")
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)

    result = job_details.fetch_full_job_description_result(
        _job(jd_url="https://example.test/jobs")
    )
    diagnostic = result.identity_diagnostic

    assert result.status == "identity_ambiguous"
    assert diagnostic["status"] == "identity_ambiguous"
    assert set(diagnostic["observed_job_ids"]) == {"101", "102"}
    assert diagnostic["observed_titles"] == ["C++工程师"]
    assert diagnostic["failed_step"] == "render:identity_ambiguous"


def test_identity_diagnostic_redacts_and_bounds_exception_detail() -> None:
    result = job_details._detail_result(
        _job(),
        "",
        "identity_mismatch",
        source="render",
        detail_url=_job()["jd_url"],
        attempts=("render:identity_mismatch",),
        error_type="RuntimeError",
        error_detail='token=secret-value body={"token":"body-secret"} ' + "x" * 400,
    )

    exception = result.identity_diagnostic["exception"]
    assert exception["type"] == "RuntimeError"
    assert "secret-value" not in exception["detail"]
    assert "body-secret" not in exception["detail"]
    assert "token=<redacted>" in exception["detail"]
    assert "body=<redacted>" in exception["detail"]
    assert len(exception["detail"]) <= 240
    assert result.error_detail == exception["detail"]


@pytest.mark.parametrize("heading,status", [("C++工程师", "complete"), ("会计", "identity_mismatch")])
def test_rendered_visible_heading_binds_only_requested_job(monkeypatch, heading, status) -> None:
    page = f"<main><h1>{heading}</h1><h2>岗位职责</h2>{UNTITLED_COMPLETE_JD}</main>"
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)
    result = job_details.fetch_full_job_description_result(_job())
    assert result.status == status
    assert bool(result.detail) is (status == "complete")
    assert result.source == "render"
    assert result.detail_url == _job()["jd_url"]
    assert result.attempts[-1] == f"render:{status}"


def test_rendered_post_name_binds_observed_query_job_id(monkeypatch) -> None:
    title = "【2027校招】游戏运营培训生"
    url = "https://hr.example.test/weixin/?r=job/view&id=JO20260831002&type=agent"
    page = (
        f'<main><span class="post_name">{title}</span>'
        f'<section><h3>岗位职责</h3>{UNTITLED_COMPLETE_JD}</section></main>'
    )
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)

    result = job_details.fetch_full_job_description_result(
        _job(title=title, jd_url=url, source_job_id="JO20260831002")
    )

    assert result.complete
    assert result.identity_status == "matched"
    assert f"title:{title}" in result.identity_evidence


@pytest.mark.parametrize("field,value", [("data-job-id", "102"), ("data-company", "Other")])
def test_scoped_card_rejects_conflicting_native_id_or_company(monkeypatch, field, value) -> None:
    page = _card("C++工程师", "101").replace(
        f'{field}="' + ("101" if field == "data-job-id" else "Example") + '"',
        f'{field}="{value}"',
    )
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)
    result = job_details.fetch_full_job_description_result(_job(native_job_id="101"))
    assert result.status == "identity_mismatch"
    assert result.detail == ""


def test_shared_list_returns_only_uniquely_scoped_card(monkeypatch) -> None:
    page = _captured_page(
        _card("会计", "102", "岗位职责：负责会计核算。" * 40) + _card("C++工程师", "101"),
    )
    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response(text=page))
    monkeypatch.setattr(job_details, "_configured_careers_urls", lambda: {})
    job = _job(jd_url="https://example.test/jobs", link_kind="list", native_job_id="101")
    result = job_details.fetch_full_job_description_result(job)
    assert result.complete
    assert "会计核算" not in result.detail
    assert "负责机器人控制软件" in result.detail
    assert result.detail_url == job["jd_url"]
    assert job["link_kind"] == "list"


@pytest.mark.parametrize("page", [
    _card("C++工程师", "101") + _card("C++工程师", "102"),
    "<main><h2>C++工程师</h2><h2>会计</h2><h3>岗位职责</h3>" + UNTITLED_COMPLETE_JD + "</main>",
])
def test_multijob_page_without_unique_detail_binding_is_ambiguous(monkeypatch, page) -> None:
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)
    result = job_details.fetch_full_job_description_result(_job(jd_url="https://example.test/jobs"))
    assert result.status == "identity_ambiguous"
    assert result.detail == ""


@pytest.mark.parametrize("field", ["careers_url", "campaign_url", "source_url", "list_url"])
def test_job_provided_list_precedes_legacy_config_and_resolves_detail(monkeypatch, field) -> None:
    calls = []
    source = "https://example.test/campus"
    resolved = "https://example.test/job/101?locale=zh"

    def get(url, **_kwargs):
        calls.append(url)
        if url == source:
            return Response(text='<main><a href="/job/102">会计</a><a href="/job/101">C++工程师</a></main>')
        assert url == "https://example.test/job/101"
        return Response(text=_card("C++工程师", "101"), url=resolved)

    monkeypatch.setattr(job_details.requests, "get", get)
    monkeypatch.setattr(job_details, "_configured_careers_urls", lambda: {"Example": "https://legacy.test/jobs"})
    result = job_details.fetch_full_job_description_result(
        _job(jd_url="https://example.test/old-list", link_kind="list", **{field: source})
    )
    assert result.complete
    assert result.detail_url == resolved
    assert result.source == "configured_page"
    assert calls == [source, "https://example.test/job/101"]
    assert len(result.attempts) == 2


@pytest.mark.parametrize("text", ["公司欢迎优秀人才加入，共创未来。" * 60, "工作地点 上海 发布于 2026-09-01 招聘类型 校园招聘 全职 申请 收藏 " * 20])
def test_long_untitled_marketing_or_metadata_is_not_complete(text) -> None:
    assert job_details.is_jd_incomplete(_job(jd_raw=text))


@pytest.mark.parametrize("field,value", [("title", "会计"), ("id", "102"), ("companyName", "Other")])
@pytest.mark.parametrize("platform", ["feishu", "beisen", "tencent", "moka"])
def test_platform_identity_conflict_is_terminal(monkeypatch, platform, field, value) -> None:
    url, payload = _platform_response(platform, {field: value})
    job_details._moka_site_context.cache_clear()
    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response(text=MOKA_SITE_HTML, payload=payload))
    monkeypatch.setattr(job_details.requests, "post", lambda *_args, **_kwargs: Response(payload=payload))
    result = job_details.fetch_full_job_description_result(_job(jd_url=url))
    if platform == "tencent" and field == "id":
        # Tencent's ``id`` is an internal namespace; the URL-bound postId is
        # the identity that must be validated for this endpoint.
        assert result.complete
        assert result.identity_status == "request_bound"
    else:
        assert result.status == "identity_mismatch"
        assert result.detail == ""
        assert result.identity_status == "mismatch"
        assert result.source != "render"


def _platform_response(platform: str, identity: dict) -> tuple[str, dict]:
    if platform == "moka":
        return (
            f"https://app.mokahr.com/campus-recruitment/demo/54046#/job/{MOKA_JOB_ID}",
            {"code": 0, "data": {"jobDescription": UNTITLED_COMPLETE_JD, **identity}},
        )
    if platform == "beisen":
        return "https://example.zhiye.com/2/detail?jobAdId=101", {"Data": {"Duty": UNTITLED_COMPLETE_JD, **identity}}
    if platform == "tencent":
        return "https://join.qq.com/post.html?postId=101", {"code": 0, "data": {"desc": UNTITLED_COMPLETE_JD, **identity}}
    return "https://example.jobs.feishu.cn/position/101/detail", {"data": {"job_post_detail": {"description": UNTITLED_COMPLETE_JD, **identity}}}


@pytest.mark.parametrize("platform", ["feishu", "beisen", "tencent", "moka"])
@pytest.mark.parametrize("with_identity", [True, False])
def test_platform_stable_request_accepts_correct_or_absent_identity(monkeypatch, platform, with_identity) -> None:
    identity = {"id": MOKA_JOB_ID if platform == "moka" else "101", "title": "C++工程师", "companyName": "Example"} if with_identity else {}
    url, payload = _platform_response(platform, identity)
    job_details._moka_site_context.cache_clear()
    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response(text=MOKA_SITE_HTML, payload=payload))
    monkeypatch.setattr(job_details.requests, "post", lambda *_args, **_kwargs: Response(payload=payload))
    result = job_details.fetch_full_job_description_result(_job(jd_url=url))
    assert result.complete
    expected_identity = "matched" if with_identity and platform in {"moka", "tencent"} else "request_bound"
    assert result.identity_status == expected_identity
    assert result.detail_url == url


@pytest.mark.parametrize("conflict", [False, True])
def test_huawei_advertisement_and_underlying_job_ids_have_distinct_namespaces(monkeypatch, conflict) -> None:
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        if "getRecruitmentPositionDetail" in url:
            return Response(payload={"data": {"advertisementId": "101", "jobId": "internal-2", "jobCnName": "C++工程师"}})
        return Response(payload={"data": [{"jobId": "wrong" if conflict else "internal-2", "positionIntention": "软件研发", "jobResponsibilities": UNTITLED_COMPLETE_JD}]})

    monkeypatch.setattr(job_details.requests, "post", post)
    result = job_details.fetch_full_job_description_result(_job(jd_url="https://career.huawei.com/reccampportal/portal5/campus-recruitment-detail.html?advertisementId=101", native_job_id="101"))
    assert result.status == ("identity_mismatch" if conflict else "complete")
    assert bool(result.detail) is not conflict
    assert calls == [{"advertisementId": "101"}, {"jobId": "internal-2"}]


@pytest.mark.parametrize("variant", ["jobDescription", "description", "content"])
@pytest.mark.parametrize("id_field", ["id", "jobId", "absent"])
def test_moka_clear_response_field_variants_regression(monkeypatch, variant, id_field) -> None:
    job_details._moka_site_context.cache_clear()
    data = {variant: UNTITLED_COMPLETE_JD, "title": "C++工程师"}
    if id_field != "absent":
        data[id_field] = MOKA_JOB_ID
    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response(text=MOKA_SITE_HTML))
    monkeypatch.setattr(job_details.requests, "post", lambda *_args, **_kwargs: Response(payload={"code": 0, "data": data}))
    result = job_details.fetch_full_job_description_result(_job(jd_url=f"https://app.mokahr.com/campus_apply/demo/54046#/job/{MOKA_JOB_ID}"))
    assert result.complete


@pytest.mark.parametrize("conflict", [False, True])
def test_moka_list_fallback_validates_title_and_retains_attempts(monkeypatch, conflict) -> None:
    job_details._moka_site_context.cache_clear()
    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response(text=MOKA_SITE_HTML))

    def post(url, **_kwargs):
        if url.endswith("/website/job"):
            return Response(payload={"unexpected": "variant"})
        return Response(payload={"code": 0, "data": {"jobs": [{"id": "other", "title": "C++工程师", "jobDescription": "WRONG FIRST JD"}, {"id": MOKA_JOB_ID, "title": "会计" if conflict else "C++工程师", "jobDescription": UNTITLED_COMPLETE_JD}]}})

    monkeypatch.setattr(job_details.requests, "post", post)
    result = job_details.fetch_full_job_description_result(_job(jd_url=f"https://app.mokahr.com/campus_apply/demo/54046#/job/{MOKA_JOB_ID}"))
    assert result.status == ("identity_mismatch" if conflict else "complete")
    assert "WRONG FIRST JD" not in result.detail
    assert result.attempts[0] == "moka_detail_api:api_variant_unsupported"
    assert result.attempts[-1] == f"moka_list_api:{result.status}"
    assert result.error_type == "_MokaApiVariantUnsupported"


def test_moka_direct_html_conflicting_heading_cannot_fall_back_to_whole_page(monkeypatch) -> None:
    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response(text="<main><h1>会计</h1>" + UNTITLED_COMPLETE_JD + "</main>"))
    result = job_details.fetch_full_job_description_result(_job(jd_url=f"https://app.mokahr.com/job/{MOKA_JOB_ID}"))
    assert result.status == "identity_mismatch"
    assert result.detail == ""


def test_list_detail_fetch_timeout_retains_resolved_target_and_diagnostics(monkeypatch) -> None:
    def get(url, **_kwargs):
        if url.endswith("/jobs"):
            return Response(text='<a href="/job/101">C++工程师</a>')
        raise requests.ReadTimeout("fixture timeout")

    monkeypatch.setattr(job_details.requests, "get", get)
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: None)
    result = job_details.fetch_full_job_description_result(_job(jd_url="https://example.test/jobs", link_kind="list"))
    assert result.status == "timeout"
    assert result.detail_url == "https://example.test/job/101"
    assert result.source == "configured_page"
    assert result.error_type == "ReadTimeout"
    assert result.attempts == (
        "configured_page:detail_link",
        "configured_page:timeout",
        "configured_page_render:render_failed",
    )


def test_same_jd_text_for_distinct_bound_jobs_is_not_a_failure(monkeypatch) -> None:
    page = _captured_page(_card("C++工程师", "101") + _card("机器人软件工程师", "102"))
    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response(text=page))
    results = [job_details.fetch_full_job_description_result(_job(title=title, native_job_id=native_id, jd_url="https://example.test/jobs", link_kind="list")) for title, native_id in [("C++工程师", "101"), ("机器人软件工程师", "102")]]
    assert all(result.complete for result in results)
    assert all(UNTITLED_COMPLETE_JD in result.detail for result in results)


def test_result_positional_defaults_and_string_compatibility() -> None:
    result = job_details.JobDetailHydrationResult("detail", "complete", "render", "url", ("render:complete",), "")
    assert result.complete
    assert result.identity_status == ""
    assert result.identity_evidence == ()
    assert job_details.fetch_full_job_description(
        _job(
            jd_raw=UNTITLED_COMPLETE_JD,
            capture_evidence=_capture_evidence(UNTITLED_COMPLETE_JD),
        )
    ) == UNTITLED_COMPLETE_JD


def test_identity_errors_cannot_be_promoted_by_complete_text() -> None:
    for status in ("identity_mismatch", "identity_ambiguous"):
        result = job_details._detail_result(_job(), UNTITLED_COMPLETE_JD, status, source="fixture", detail_url="url")
        assert result.status == status
        assert result.detail == ""


@pytest.mark.parametrize("platform,url", [
    ("feishu_api", "https://example.jobs.feishu.cn/campus/position/101/detail"),
    ("moka_official", f"https://app.mokahr.com/campus_apply/demo/123#/job/{MOKA_JOB_ID}"),
])
@pytest.mark.parametrize("page,expected", [
    ("<main><h1>C++工程师</h1>" + UNTITLED_COMPLETE_JD + "</main>", "complete"),
    (None, "render_failed"),
    ("<main><h1>Accountant</h1>" + UNTITLED_COMPLETE_JD + "</main>", "identity_mismatch"),
])
def test_platform_proxy_failure_survives_render_fallback_diagnostics(monkeypatch, platform, url, page, expected) -> None:
    def proxy_failure(*_args, **_kwargs):
        raise requests.exceptions.ProxyError("local proxy connection refused")

    monkeypatch.setattr(job_details.requests, "get", proxy_failure)
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)
    job_details._moka_site_context.cache_clear()
    result = job_details.fetch_full_job_description_result(
        _job(jd_url=url)
    )
    assert result.status == expected
    assert result.error_type == "ProxyError"
    assert result.attempts == (f"{platform}:fetch_failed", f"render:{expected}")
    assert result.source == "render"


@pytest.mark.parametrize("heading,expected", [("C++工程师", "complete"), ("会计", "identity_mismatch")])
def test_hotjob_sticky_apply_title_and_category_badge_are_not_separate_jobs(monkeypatch, heading, expected) -> None:
    page = (
        f'<div class="pos-detail-wrap"><div class="fixed-postInfo"><p class="tit">{heading}</p>立即投递</div>'
        f'<div class="pos-detail-hd__titBar"><span class="tit">{heading}<span>智能制造类</span></span></div>'
        f'<div>工作职责\n{UNTITLED_COMPLETE_JD}</div></div>'
    )
    monkeypatch.setattr(job_details, "fetch_hotjob_position_detail", lambda url: ("", url))
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)
    result = job_details.fetch_full_job_description_result(
        _job(jd_url="https://wecruit.hotjob.cn/SU123/pb/posDetail.html?postId=101", native_job_id="101")
    )
    assert result.status == expected
    assert bool(result.detail) is (expected == "complete")
    assert result.attempts == ("hotjob_api:fetch_failed", f"render:{expected}")


@pytest.mark.parametrize("conflict", ["sticky_native_id", "multiple_detail_titles"])
def test_hotjob_duplicate_title_normalization_does_not_hide_identity_conflicts(conflict) -> None:
    heading = '<div class="pos-detail-hd__titBar"><span class="tit">C++工程师<span>智能制造类</span></span></div>'
    sticky = '<div class="fixed-postInfo" data-job-id="102"><p class="tit">C++工程师</p></div>'
    page = sticky + heading if conflict == "sticky_native_id" else heading * 2
    page += "<div>工作职责\n" + UNTITLED_COMPLETE_JD + "</div>"
    job = _job(jd_url="https://wecruit.hotjob.cn/SU123/pb/posDetail.html?postId=101", native_job_id="101")
    result = job_details._extract_scoped_jd(page, job, detail_url=job["jd_url"])
    assert result.status in {"identity_mismatch", "identity_ambiguous"}
    assert result.detail == ""


def _pipeline_normalized_detail_job(url: str) -> dict:
    from packages.domain.job_identity import build_job_identity
    from packages.pipeline.daily import PipelineCompany, _observed_job

    company = PipelineCompany("fixture-company", "Example", url, "render", "connected")
    raw = _job(jd_url=url, cohort=2027, cohort_status="confirmed", batch="formal", city="上海  北京")
    _, job = _observed_job(company, raw)
    job["native_job_id"] = build_job_identity(company.crawler_config(), job).native_job_id
    return job


@pytest.mark.parametrize("source_id", ["", "101", "102", "f" * 64])
def test_pipeline_generated_native_hash_is_not_an_ats_id_but_source_conflicts_remain(monkeypatch, source_id) -> None:
    url = "https://example.jobs.feishu.cn/campus/position/101/detail"
    job = _pipeline_normalized_detail_job(url)
    assert job["native_job_id"] == job["id"]
    assert len(job["id"]) == 64
    job["source_job_id"] = source_id
    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response(payload={"data": {"job_post_detail": {"id": "101", "title": job["title"], "description": UNTITLED_COMPLETE_JD}}}))
    result = job_details.fetch_full_job_description_result(job)
    assert result.status == ("complete" if source_id in {"", "101"} else "identity_mismatch")
    assert result.source == ("feishu_api" if source_id in {"", "101"} else "request")


@pytest.mark.parametrize("host", ["app.mokahr.com/campus_apply/demo/54046", "campus.example.test"])
def test_pipeline_generated_native_hash_reaches_uuid_detail_reader(monkeypatch, host) -> None:
    url = f"https://{host}/#/job/{MOKA_JOB_ID}"
    job = _pipeline_normalized_detail_job(url)
    job_details._moka_site_context.cache_clear()
    monkeypatch.setattr(job_details.requests, "get", lambda *_args, **_kwargs: Response(text=MOKA_SITE_HTML))
    monkeypatch.setattr(job_details.requests, "post", lambda *_args, **_kwargs: Response(payload={"code": 0, "data": {"id": MOKA_JOB_ID, "title": job["title"], "jobDescription": UNTITLED_COMPLETE_JD}}))
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: f'<main><h1>{job["title"]}</h1>{UNTITLED_COMPLETE_JD}</main>')
    result = job_details.fetch_full_job_description_result(job)
    assert result.complete
    assert result.source != "request"


def test_unproven_64_character_native_id_is_not_silently_ignored() -> None:
    job = _pipeline_normalized_detail_job("https://example.test/job/101")
    job.update(id="a" * 64, native_job_id="a" * 64)
    result = job_details.fetch_full_job_description_result(job)
    assert result.status == "identity_mismatch"
    assert result.source == "request"


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("conflict", ["", "title", "native_id", "request_id", "other_description", "multiple_headers"])
def test_moka_rendered_detail_layout_is_scoped_and_identity_checked(modern, conflict) -> None:
    url = f"https://campus.example.test/campus_apply/demo/123/#/job/{MOKA_JOB_ID}"
    title = "Accountant" if conflict == "title" else "C++工程师"
    heading = f'<div class="title-fixture">{title}</div>'
    header = f'<div class="{"header-wrapper-new" if modern else "job-info-old"}">{heading}</div>'
    if conflict == "multiple_headers":
        header *= 2
    description = f'<div class="job-description-fixture">{UNTITLED_COMPLETE_JD}</div>'
    if modern:
        description *= 2
    if conflict == "other_description":
        description += '<div class="job-description-fixture">Different job description</div>'
    data_id = 'data-job-id="wrong-id"' if conflict == "native_id" else ""
    layout = "left-panel-new" if modern else "job-details-old"
    page = f'<div class="{layout}" {data_id}>{header}{description}</div>'
    page += '<aside><a href="#/job/other">Other role</a></aside>'
    job = _job(jd_url=url)
    if conflict == "request_id":
        job["native_job_id"] = "wrong-request-id"
    result = job_details._extract_scoped_jd(page, job, detail_url=url)
    if not conflict:
        assert result.complete
        assert result.detail == UNTITLED_COMPLETE_JD
        assert result.identity_status == "matched"
        assert "title:C++工程师" in result.identity_evidence
    else:
        assert result.status in {"identity_mismatch", "identity_ambiguous"}
        assert not result.detail
