from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import quote

import pytest
import requests

from packages.recruitment_core import job_details
from packages.recruitment_core.crawlers.render import render_page
from packages.recruitment_core.jd_capture import assess_jd_capture


class Response:
    def __init__(self, *, text: str = "", payload: object = None, url: str = "") -> None:
        self.text = text
        self._payload = payload
        self.url = url
        self.apparent_encoding = "utf-8"
        self.encoding = "utf-8"
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def _job(**overrides: object) -> dict:
    return {
        "title": "机器人软件工程师",
        "company": "Example",
        "jd_raw": "",
        "jd_url": "https://example.test/job/101",
        **overrides,
    }


def _capture_page(*, title: str = "机器人软件工程师", job_id: str = "101", evidence: bool = True) -> str:
    attrs = (
        ' data-recruitops-capture-status="complete"'
        ' data-recruitops-capture-method="detail_interaction:inline"'
        ' data-recruitops-terminal-observed="true"'
        ' data-recruitops-remaining-controls="[]"'
        if evidence
        else ""
    )
    return (
        f"<html{attrs}><main><article data-job-id=\"{job_id}\">"
        f"<h1>{title}</h1><section><h2>职位描述</h2><p>负责机器人控制。</p></section>"
        "</article></main></html>"
    )


def test_short_official_api_detail_has_strict_capture_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    short_detail = "岗位职责\n负责机器人控制。"
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(
            payload={
                "code": 0,
                "data": {
                    "job_post_detail": {
                        "id": "101",
                        "title": "机器人软件工程师",
                        "description": "负责机器人控制。",
                    }
                },
            }
        ),
    )

    result = job_details.fetch_full_job_description_result(
        _job(jd_url="https://example.jobs.feishu.cn/position/101/detail", source_job_id="101")
    )

    assert result.status == "complete"
    expected_evidence = {
        "status": "complete",
        "method": "official_api",
        "source_url": "https://example.jobs.feishu.cn/position/101/detail",
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(short_detail.strip().encode("utf-8")).hexdigest(),
    }
    assert {
        key: result.capture_evidence[key]
        for key in expected_evidence
    } == expected_evidence
    captured_at = datetime.fromisoformat(result.capture_evidence["captured_at"])
    assert captured_at.tzinfo is not None
    assert captured_at.utcoffset() == timezone.utc.utcoffset(captured_at)
    assert assess_jd_capture({"jd_raw": result.detail, "capture_evidence": result.capture_evidence}).complete


def test_semantically_complete_local_text_without_capture_is_refetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def render(url: str, **_kwargs: object) -> str:
        calls.append(url)
        return _capture_page(evidence=False)

    monkeypatch.setattr(job_details, "render_page", render)
    result = job_details.fetch_full_job_description_result(
        _job(jd_raw="岗位职责\n" + "负责机器人控制软件开发。" * 30)
    )

    assert result.source == "render"
    assert calls == ["https://example.test/job/101"]
    assert result.complete


def test_detail_result_never_promotes_semantic_richness_without_capture_proof() -> None:
    result = job_details._detail_result(
        _job(),
        "岗位职责\n" + "负责机器人控制软件开发。" * 30,
        "complete",
        source="fixture",
        detail_url="https://example.test/job/101",
        identity_status="matched",
        identity_evidence=("title:机器人软件工程师",),
    )

    assert result.status == "content_incomplete"
    assert result.capture_evidence["status"] == "incomplete"


def test_unique_detail_dom_allows_short_content_without_section_richness_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: _capture_page(evidence=False))

    result = job_details.fetch_full_job_description_result(_job(native_job_id="101"))

    assert result.complete
    assert result.capture_evidence["method"] == "detail_dom"
    assert result.capture_evidence["identity_verified"] is True
    assert result.capture_evidence["terminal_observed"] is True
    assert result.capture_evidence["remaining_controls"] == []
    assert assess_jd_capture({"jd_raw": result.detail, "capture_evidence": result.capture_evidence}).complete


@pytest.mark.parametrize("mode", ["inline", "dialog", "tabs"])
def test_explicit_interaction_recipe_is_forwarded_and_binds_target(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    recipe = {
        "mode": mode,
        "trigger_selector": ".job-card .detail-trigger",
        "container_selector": ".job-detail",
        **({"tab_selectors": ["[role='tab']"]} if mode == "tabs" else {}),
    }
    calls: dict[str, object] = {}

    def render(_url: str, **kwargs: object) -> str:
        calls.update(kwargs)
        return _capture_page(evidence=True)

    monkeypatch.setattr(job_details, "render_page", render)
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(text="<main><article data-job-id='101'>卡片</article></main>"),
    )
    result = job_details.fetch_full_job_description_result(
        _job(
            jd_url="https://example.test/jobs",
            link_kind="list",
            careers_url="https://example.test/jobs",
            native_job_id="101",
            detail_interaction=recipe,
        )
    )

    assert calls["detail_interaction"] == recipe
    assert calls["detail_title"] == "机器人软件工程师"
    assert calls["detail_job_id"] == "101"
    assert result.complete
    assert result.capture_evidence["method"] == "detail_interaction:inline"


def test_interaction_can_explicitly_bind_a_list_card_by_unique_title_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        job_details,
        "render_page",
        lambda _url, **kwargs: calls.update(kwargs) or _capture_page(job_id="", evidence=True),
    )
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(text="<main><article>卡片</article></main>"),
    )

    result = job_details.fetch_full_job_description_result(_job(
        jd_url="https://example.test/jobs",
        link_kind="list",
        careers_url="https://example.test/jobs",
        native_job_id="internal-only-101",
        detail_interaction={
            "mode": "inline",
            "trigger_selector": ".job-card",
            "container_selector": ".job-card",
            "bind_job_id": False,
        },
    ))

    assert calls["detail_job_id"] == ""
    assert result.complete


@pytest.mark.parametrize("mode", ["dialog", "tabs"])
def test_interaction_capture_rejects_identity_mismatch(mode: str) -> None:
    recipe_attrs = (
        ' data-recruitops-capture-status="complete"'
        ' data-recruitops-capture-method="detail_interaction:' + mode + '"'
        ' data-recruitops-terminal-observed="true"'
        ' data-recruitops-remaining-controls="[]"'
    )
    page = (
        f"<html{recipe_attrs}><main><article data-job-id='102'>"
        "<h1>会计</h1><h2>职位描述</h2><p>负责会计核算。</p>"
        "</article></main></html>"
    )
    result = job_details._extract_scoped_jd(
        page,
        _job(native_job_id="101"),
        detail_url="https://example.test/jobs",
        is_list=True,
    )

    assert result.status in {"identity_mismatch", "identity_ambiguous"}
    assert result.detail == ""
    assert result.capture_evidence.get("status") != "complete"


def test_unexpanded_card_does_not_become_complete_from_long_or_structured_text() -> None:
    page = (
        "<main><article data-job-id='101'><h2>机器人软件工程师</h2>"
        "<h3>职位描述</h3><p>负责机器人控制软件开发。</p></article></main>"
    )
    result = job_details._extract_scoped_jd(
        page,
        _job(native_job_id="101"),
        detail_url="https://example.test/jobs",
        is_list=True,
    )

    assert result.status == "content_incomplete"
    assert result.detail == ""
    assert result.capture_evidence.get("status") != "complete"


def test_visible_scope_controls_block_dom_and_moka_completion() -> None:
    url = "https://app.mokahr.com/job/78086983-48dd-4914-bbc1-90302918825b"
    page = """
    <div class="job-details-old">
      <div class="job-info-old"><div class="title-fixture">Robot Engineer</div></div>
      <div class="job-description-fixture">
        <p>Ten chars.</p>
        <button aria-expanded="false">Show more</button>
      </div>
    </div>
    """

    result = job_details._extract_scoped_jd(
        page,
        _job(title="Robot Engineer", jd_url=url),
        detail_url=url,
        source="render",
    )

    assert result.status == "content_incomplete"
    assert result.capture_evidence["status"] == "incomplete"
    assert "Show more" in result.capture_evidence["remaining_controls"]


def test_interaction_timeout_is_not_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*_args: object, **_kwargs: object) -> None:
        raise requests.Timeout("detail interaction timeout")

    monkeypatch.setattr(job_details, "render_page", timeout)
    result = job_details.fetch_full_job_description_result(
        _job(
            detail_interaction={
                "mode": "dialog",
                "trigger_selector": ".job-card .detail-trigger",
                "container_selector": ".job-detail",
            }
        )
    )

    assert result.status == "timeout"
    assert result.capture_evidence.get("status") != "complete"
    assert result.capture_evidence.get("terminal_observed") is not True


def test_unresolved_tabs_do_not_promote_an_unannotated_detail_dom() -> None:
    page = (
        "<main><article data-job-id='101'><h1>机器人软件工程师</h1>"
        "<h2>职位描述</h2><p>负责机器人控制。</p></article></main>"
    )
    result = job_details._extract_scoped_jd(
        page,
        _job(
            native_job_id="101",
            detail_interaction={
                "mode": "tabs",
                "trigger_selector": ".job-card .detail-trigger",
                "container_selector": ".job-detail",
                "tab_selectors": ["[role='tab']"],
            },
        ),
        detail_url="https://example.test/job/101",
    )

    assert result.status == "content_incomplete"
    assert result.capture_evidence.get("status") != "complete"


def _local_dom_url(body: str) -> str:
    return "data:text/html," + quote(
        "<!doctype html><html><body>" + body + "</body></html>",
        safe="",
    )


def _interaction_dom(mode: str) -> str:
    if mode == "inline":
        return """
        <article class="job-card" data-job-id="101">
          <h2>Robot Engineer</h2>
          <button class="detail-trigger">View details</button>
          <div class="job-detail" hidden><h2>Job Description</h2><p>Controls robot.</p></div>
        </article>
        <script>
          document.querySelector('.detail-trigger').onclick = () => {
            document.querySelector('.job-detail').hidden = false;
          };
        </script>
        """
    if mode == "dialog":
        return """
        <article class="job-card" data-job-id="101">
          <h2>Robot Engineer</h2>
          <button class="detail-trigger">View details</button>
        </article>
        <script>
          document.querySelector('.detail-trigger').onclick = () => {
            document.body.insertAdjacentHTML('beforeend',
              '<div class="job-detail-dialog" data-job-id="101"><h1>Robot Engineer</h1><h2>Job Description</h2><p>Controls robot.</p></div>');
          };
        </script>
        """
    return """
    <article class="job-card" data-job-id="101">
      <h2>Robot Engineer</h2>
      <button class="detail-trigger">View details</button>
      <div class="job-detail" data-job-id="101" hidden>
        <h1>Robot Engineer</h1>
        <div role="tablist">
          <button role="tab" data-tab="duties">Duties</button>
          <button role="tab" data-tab="requirements">Requirements</button>
        </div>
        <div class="panel"></div>
      </div>
    </article>
    <script>
      const card = document.querySelector('.job-card');
      const detail = document.querySelector('.job-detail');
      const panel = document.querySelector('.panel');
      card.querySelector('.detail-trigger').onclick = () => { detail.hidden = false; };
      detail.querySelector('[data-tab="duties"]').onclick = () => { panel.textContent = 'Controls robot.'; };
      detail.querySelector('[data-tab="requirements"]').onclick = () => { panel.textContent = 'Familiar with C++.'; };
    </script>
    """


@pytest.mark.parametrize("mode", ["inline", "dialog", "tabs"])
def test_real_playwright_dom_interactions(mode: str) -> None:
    recipe = {
        "mode": mode,
        "trigger_selector": ".job-card .detail-trigger",
        "container_selector": ".job-detail" if mode != "dialog" else ".job-detail-dialog",
        **(
            {
                "tab_selectors": [
                    "[role='tab'][data-tab='duties']",
                    "[role='tab'][data-tab='requirements']",
                ]
            }
            if mode == "tabs"
            else {}
        ),
    }
    rendered = render_page(
        _local_dom_url(_interaction_dom(mode)),
        timeout_ms=10000,
        extra_wait_ms=0,
        wait_until="domcontentloaded",
        detail_interaction=recipe,
        detail_title="Robot Engineer",
        detail_job_id="101",
    )

    assert rendered is not None
    assert 'data-recruitops-capture-status="complete"' in rendered
    assert f'data-recruitops-capture-method="detail_interaction:{mode}"' in rendered
    assert 'data-recruitops-terminal-observed="true"' in rendered
    assert 'data-recruitops-remaining-controls="[]"' in rendered
    assert "Controls robot" in rendered or "Familiar with C++" in rendered
    if mode == "tabs":
        assert "data-recruitops-tab-captures" in rendered
        assert "Familiar with C++" in rendered


def test_real_playwright_ant_card_opens_identity_bound_drawer() -> None:
    body = """
    <script>
      setTimeout(() => {
        document.body.insertAdjacentHTML('beforeend',
          '<div class="ant-card"><h3>C++ Engineer</h3><a class="detail-trigger">View details</a><a>Select job</a></div>');
        document.querySelector('.detail-trigger').onclick = () => {
          document.body.insertAdjacentHTML('beforeend',
            '<div class="ant-drawer-content"><div class="ant-drawer-title"><span>C++ Engineer</span></div><h2>Responsibilities</h2><p>Build systems.</p><h2>Requirements</h2><p>Know C++.</p></div>');
        };
      }, 300);
    </script>
    """
    rendered = render_page(
        _local_dom_url(body),
        timeout_ms=10000,
        extra_wait_ms=0,
        wait_until="domcontentloaded",
        detail_interaction={
            "mode": "dialog",
            "trigger_selector": ".ant-card a",
            "trigger_text": "View details",
            "container_selector": ".ant-drawer-content",
        },
        detail_title="C++ Engineer",
        detail_job_id="",
    )

    assert rendered is not None
    assert 'data-recruitops-capture-status="complete"' in rendered

    result = job_details._extract_scoped_jd(
        rendered,
        _job(
            title="C++ Engineer",
            native_job_id="synthetic-list-id",
            detail_interaction={
                "mode": "dialog",
                "trigger_selector": ".ant-card a",
                "trigger_text": "View details",
                "container_selector": ".ant-drawer-content",
                "bind_job_id": False,
            },
        ),
        detail_url="https://example.test/campus",
        source="configured_page_render",
        is_list=True,
    )

    assert result.status == "complete"
    assert "Build systems." in result.detail
    assert "Know C++." in result.detail
    assert 'data-recruitops-capture-method="detail_interaction:dialog"' in rendered
    assert "Know C++" in rendered


def test_real_playwright_verified_container_allows_ten_char_untitled_detail() -> None:
    body = """
    <article class="job-card" data-job-id="101" data-job-title="Robot Engineer">
      <button class="detail-trigger">View details</button>
      <div class="job-detail" data-job-id="101" hidden>
        <h1>Robot Engineer</h1>
        <p>Ten chars.</p>
      </div>
    </article>
    <script>
      const card = document.querySelector('.job-card');
      const detail = document.querySelector('.job-detail');
      card.querySelector('.detail-trigger').onclick = () => { detail.hidden = false; };
    </script>
    """
    recipe = {
        "mode": "inline",
        "trigger_selector": ".job-card .detail-trigger",
        "container_selector": ".job-detail",
    }
    rendered = render_page(
        _local_dom_url(body),
        timeout_ms=10000,
        extra_wait_ms=0,
        wait_until="domcontentloaded",
        detail_interaction=recipe,
        detail_title="Robot Engineer",
        detail_job_id="101",
    )

    assert rendered is not None
    result = job_details._extract_scoped_jd(
        rendered,
        _job(title="Robot Engineer", native_job_id="101", detail_interaction=recipe),
        detail_url="https://example.test/job/101",
        source="render",
    )

    assert result.status == "complete"
    assert result.detail == "Ten chars."
    assert result.capture_evidence["method"] == "detail_interaction:inline"
