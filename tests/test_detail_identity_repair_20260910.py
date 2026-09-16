from __future__ import annotations

from typing import Any

import pytest

from packages.recruitment_core import job_details


class Response:
    def __init__(
        self,
        payload: object,
        *,
        url: str = "",
        status_code: int = 200,
        text: str = "",
    ) -> None:
        self._payload = payload
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


POST_ID = "1274356448142695424"
TENCENT_TITLE = "腾讯营销—多模态大模型与强化学习驱动的广告投放Agent"
MOKA_ID = "08c9175d-ccab-4a25-b59e-2541e7fb6822"
MOKA_TITLE = "急 测试开发工程师（2027届）"
MOKA_API_TITLE = "测试开发工程师（2027届）"
MOKA_SITE_URL = "https://app.mokahr.com/campus_apply/yanhun/24017"
BEISEN_URL = "https://cssc.zhiye.com/zpdetail/311170879?PageIndex=4"
BEISEN_TITLE = "软件研发工程师(J12031)"


def _job(**overrides: Any) -> dict[str, Any]:
    return {
        "company": "Example",
        "title": "机器人软件工程师",
        "jd_raw": "",
        "jd_url": "https://example.test/job/101",
        **overrides,
    }


def _tencent_payload(
    *,
    post_id: object = POST_ID,
    internal_id: object = 761,
    title: object = TENCENT_TITLE,
) -> dict[str, Any]:
    return {
        "code": 0,
        "data": {
            "postId": post_id,
            "id": internal_id,
            "title": title,
            "desc": "研究多模态模型与强化学习，负责广告投放 Agent 的算法开发。",
            "request": "熟悉大模型训练、强化学习和 Python 工程实践。",
        },
    }


def test_tencent_post_id_is_authoritative_and_internal_id_is_diagnostic(monkeypatch) -> None:
    url = f"https://join.qq.com/post_detail.html?postid={POST_ID}"
    calls: list[tuple[str, dict[str, Any]]] = []

    def get(request_url: str, **kwargs: Any) -> Response:
        calls.append((request_url, kwargs))
        return Response(_tencent_payload(), url=request_url)

    monkeypatch.setattr(job_details.requests, "get", get)
    result = job_details.fetch_full_job_description_result(
        _job(
            company="腾讯-青云计划",
            title=TENCENT_TITLE,
            jd_url=url,
            source_job_id=POST_ID,
            source_post_id=POST_ID,
        )
    )

    assert result.complete
    assert result.source == "tencent_api"
    assert result.identity_status == "matched"
    assert f"native_id:{POST_ID}" in result.identity_evidence
    assert f"post_id:{POST_ID}" in result.identity_evidence
    assert "internal_job_id:761" in result.identity_evidence
    assert f"title:{TENCENT_TITLE}" in result.identity_evidence
    assert calls[0][0].endswith("/api/v1/jobDetails/getJobDetailsByPostId")
    assert calls[0][1]["params"]["postId"] == POST_ID


def test_tencent_missing_post_id_does_not_become_identity_conflict(monkeypatch) -> None:
    url = f"https://join.qq.com/post_detail.html?postid={POST_ID}"
    payload = _tencent_payload()
    payload["data"].pop("postId")
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(payload),
    )

    result = job_details.fetch_full_job_description_result(
        _job(title=TENCENT_TITLE, jd_url=url, source_job_id=POST_ID)
    )

    assert result.complete
    assert result.status != "identity_mismatch"
    assert "internal_job_id:761" in result.identity_evidence
    assert f"title:{TENCENT_TITLE}" in result.identity_evidence


@pytest.mark.parametrize(
    ("post_id", "title", "observed_id", "observed_title"),
    [
        ("1274356448142695999", TENCENT_TITLE, "1274356448142695999", TENCENT_TITLE),
        (POST_ID, "另一个岗位", POST_ID, "另一个岗位"),
    ],
)
def test_tencent_positive_identity_conflicts_keep_actual_diagnostics(
    monkeypatch,
    post_id: str,
    title: str,
    observed_id: str,
    observed_title: str,
) -> None:
    url = f"https://join.qq.com/post_detail.html?postid={POST_ID}"
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(
            _tencent_payload(post_id=post_id, title=title)
        ),
    )

    result = job_details.fetch_full_job_description_result(
        _job(title=TENCENT_TITLE, jd_url=url, source_job_id=POST_ID)
    )

    assert result.status == "identity_mismatch"
    assert result.detail == ""
    diagnostic = result.identity_diagnostic
    assert observed_id in diagnostic["observed_job_ids"]
    assert "761" in diagnostic["observed_job_ids"]
    assert observed_title in diagnostic["observed_titles"]
    assert diagnostic["observation_status"] == "observed"


def test_moka_display_badge_is_ignored_but_substantive_title_conflict_is_not(monkeypatch) -> None:
    url = f"{MOKA_SITE_URL}#/job/{MOKA_ID}"
    payload = {
        "code": 0,
        "data": {
            "id": MOKA_ID,
            "title": MOKA_API_TITLE,
            "jobDescription": "岗位职责\n负责测试开发和持续优化。\n任职要求\n熟悉 Python。",
        },
    }
    monkeypatch.setattr(
        job_details,
        "_moka_site_context",
        lambda _site_url: ("yanhun", 24017, "fedcba9876543210"),
    )
    monkeypatch.setattr(
        job_details.requests,
        "post",
        lambda *_args, **_kwargs: Response(payload),
    )

    job = _job(
        company="杭州炎魂网络",
        title=MOKA_TITLE,
        jd_url=url,
        source_job_id=MOKA_ID,
    )
    accepted = job_details.fetch_full_job_description_result(job)

    assert accepted.complete
    assert accepted.identity_status == "matched"
    assert f"native_id:{MOKA_ID}" in accepted.identity_evidence
    assert f"title:{MOKA_API_TITLE}" in accepted.identity_evidence

    payload["data"]["title"] = "会计"
    rejected = job_details.fetch_full_job_description_result(job)
    assert rejected.status == "identity_mismatch"
    assert rejected.detail == ""
    assert "会计" in rejected.identity_diagnostic["observed_titles"]


def test_generic_identity_check_does_not_strip_display_noise() -> None:
    status, _ = job_details._check_identity(
        {"title": MOKA_TITLE},
        {"title": MOKA_API_TITLE},
    )
    assert status == "identity_mismatch"


def test_jd_core_adapter_uses_publish_id_and_keeps_req_id_separate(monkeypatch) -> None:
    url = "https://campus.jd.com/#/details?type=present&id=9086"
    response = Response(
        {
            "success": True,
            "body": {
                "publishId": 9086,
                "reqId": 2383,
                "positionName": "机器人软件工程师",
                "workContent": "负责核心模块开发。",
                "qualification": "掌握 Python。",
            },
        },
        url="https://campus.jd.com/api/wx/position/detail/9086",
    )
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: response,
    )

    result = job_details.fetch_full_job_description_result(
        _job(
            title="机器人软件工程师",
            jd_url=url,
            source_job_id="9086",
        )
    )

    assert result.complete
    assert result.source == "jd_official_api"
    assert "native_id:9086" in result.identity_evidence
    assert "req_id:2383" in result.identity_evidence
    assert "native_id:2383" not in result.identity_evidence


def test_beisen_legacy_route_uses_standalone_parser_and_preserves_identity(
    monkeypatch,
) -> None:
    page = f"""
    <html><body>
      <main class="STJobDetailLayout">
        <article class="STJobDetailMain">
          <div class="STJobTitle">{BEISEN_TITLE}</div>
          <section class="STJobDetailContent">
            <h3>工作职责：</h3>
            <div class="STJobDescription">负责软件研发设计相关工作。</div>
            <h3>任职资格：</h3>
            <div class="STJobDescription">硕士研究生及以上学历，2027届应届毕业生。</div>
          </section>
        </article>
      </main>
    </body></html>
    """
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(page, url=BEISEN_URL, text=page),
    )

    result = job_details.fetch_full_job_description_result(
        _job(
            company="中船集团",
            title=BEISEN_TITLE,
            jd_url=BEISEN_URL,
            source_job_id="311170879",
        )
    )

    assert result.complete
    assert result.source == "beisen_legacy_detail"
    assert result.identity_status == "matched"
    assert "route_id:311170879" in result.identity_evidence
    assert f"title:{BEISEN_TITLE}" in result.identity_evidence
    assert "软件研发设计相关工作" in result.detail


def test_beisen_legacy_missing_title_is_incomplete_not_identity_conflict(monkeypatch) -> None:
    page = "<html><body><main><h3>岗位职责</h3><p>完成软件研发工作。</p></main></body></html>"
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(page, url=BEISEN_URL, text=page),
    )

    result = job_details.fetch_full_job_description_result(
        _job(
            title=BEISEN_TITLE,
            jd_url=BEISEN_URL,
            source_job_id="311170879",
        )
    )

    assert result.status == "content_incomplete"
    assert result.identity_status == "unverified"
    assert result.detail
    assert result.status != "identity_mismatch"


def test_error_page_heading_is_not_treated_as_a_job_title(monkeypatch) -> None:
    final_url = "https://example.test/errors/404"
    page = (
        '<html data-recruitops-final-url="'
        + final_url
        + '" data-recruitops-load-state="not_found">'
        "<main><h1>嗯… 无法访问此页面</h1></main></html>"
    )
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)

    result = job_details.fetch_full_job_description_result(
        _job(jd_url="https://example.test/job/101", native_job_id="101")
    )

    assert result.detail == ""
    assert result.status != "identity_mismatch"
    assert result.detail_url == final_url
    assert job_details._capture_metadata(page)["load_state"] == "not_found"


def test_render_access_status_keeps_final_url_and_load_state(monkeypatch) -> None:
    final_url = "https://example.test/campus/login"
    page = (
        '<html data-recruitops-final-url="'
        + final_url
        + '" data-recruitops-load-state="login_required">'
        "<main><h1>请先登录</h1></main></html>"
    )
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: page)

    result = job_details.fetch_full_job_description_result(
        _job(jd_url="https://example.test/job/101", native_job_id="101")
    )

    assert result.status == "login_required"
    assert result.detail == ""
    assert result.detail_url == final_url
    assert result.capture_evidence["load_state"] == "login_required"
