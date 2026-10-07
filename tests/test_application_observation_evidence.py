"""Offline per-card identity and visible authentication evidence regressions."""

import json
import subprocess
from pathlib import Path

import pytest

from test_desktop_browser import CONTEXT, browser, node_call, observe, page


def test_short_structured_titles_and_success_without_job_keyword(page):
    _, result = observe(page, """<main>
      <div><h3>AI应用岗</h3><p>申请成功</p></div>
      <div><h3>质量专员</h3><p>2026-09-21</p><p>投递成功</p></div>
      <table><thead><tr><th>职位</th><th>状态</th></tr></thead>
      <tbody><tr><td>人事</td><td>申请成功</td></tr></tbody></table>
      </main>""")
    records = result["result"]["application_records"]
    assert [(row["title"], row["status"]) for row in records] == [
        ("人事", "applied"), ("AI应用岗", "applied"), ("质量专员", "applied")]
    assert all(row["raw_title"] == row["title"] for row in records)


def test_per_card_title_metadata_and_ids_do_not_cross_card_boundaries(page):
    _, result = observe(page, """<main class="application-records">
      <article class="application-card" data-apply-id="apply-a">
        <h3>嵌入式软件工程师 NO.2708</h3><p>2026-09-21</p>
        <p>状态：简历筛选--简历筛选</p><a href="/job?jobId=2708&token=private">查看</a>
      </article>
      <article class="application-card" data-apply-id="apply-b" data-job-id="job-b">
        <h3>游戏研发-游戏测试开发 网申第一志愿</h3><p>2026-09-22</p>
        <p>状态：简历筛选-简历筛选</p>
      </article>
      <article class="application-card"><h3>研发--测试工程师</h3>
        <p>2026-09-23</p><p>申请成功</p>
      </article></main>""")
    records = result["result"]["application_records"]
    assert len(records) == 3
    first, second, third = records
    assert first["title"] == "嵌入式软件工程师"
    assert first["raw_title"] == "嵌入式软件工程师 NO.2708"
    assert (first["job_id"], first["application_id"], first["applied_at"]) == ("2708", "apply-a", "2026-09-21")
    assert first["label"] == "简历筛选"
    assert "volunteer_index" not in first
    assert second["title"] == "游戏研发-游戏测试开发"
    assert second["raw_title"].endswith("网申第一志愿")
    assert (second["job_id"], second["application_id"], second["volunteer_index"]) == ("job-b", "apply-b", "一")
    assert second["applied_at"] == "2026-09-22"
    assert second["label"] == "简历筛选"
    assert third["title"] == "研发--测试工程师"
    assert "job_id" not in third and "application_id" not in third
    assert "apply-b" not in first["evidence"] and "2708" not in second["evidence"]
    assert "private" not in json.dumps(result)
    assert all("application_id" not in row and "job_id" not in row for row in result["result"]["entries"])


def test_navigation_and_status_copy_are_not_titles(page):
    _, result = observe(page, """<nav><article data-application-id="menu">
      <h3>职位搜索</h3><p>状态：申请成功</p></article></nav>
      <article><h3>投递成功</h3><p>2026-09-22</p><p>申请成功</p></article>
      <article><h3>欢迎查看岗位职责和任职要求</h3><p>2026-09-22</p><p>投递成功</p></article>
      <article><p>这里是招聘说明正文</p><p>2026-09-22</p><p>投递成功</p></article>""")
    assert result["result"]["application_records"] == []


def test_same_title_records_keep_distinct_stable_ids_and_ignore_conflicts(page):
    _, result = observe(page, """<main class="records">
      <article data-application-id="first"><h3>AI应用岗</h3><p>申请成功</p></article>
      <article data-application-id="second"><h3>AI应用岗</h3><p>申请成功</p></article>
      <article data-application-id="third" data-job-id="one"><h3>软件工程师</h3>
        <a href="/job?jobId=two">查看</a><p>申请成功</p></article></main>""")
    records = result["result"]["application_records"]
    assert [row["application_id"] for row in records] == ["first", "second", "third"]
    assert "job_id" not in records[-1]


def test_submission_timeline_does_not_conflict_with_current_interview(page):
    _, result = observe(page, """<article data-recruitops-application>
      <h3>AI应用岗</h3><p>申请成功</p>
      <p aria-current='step'>面试</p></article>""")
    record = result["result"]["application_records"][0]
    assert record["status"] == "interview" and record["label"] == "面试"
    assert record["signals"]["conflicting_statuses"] is False


def test_job_title_field_precedes_other_headings_inside_card_header(page):
    _, result = observe(page, """<article data-recruitops-application>
      <header><h2 class='job-title'>AI应用岗</h2><h3>深圳分公司</h3></header>
      <p>申请成功</p></article>""")
    assert [row["title"] for row in result["result"]["application_records"]] == ["AI应用岗"]


@pytest.mark.parametrize("hidden", [
    "opacity:0", "visibility:hidden", "display:none", "content-visibility:hidden",
    "position:fixed;top:2000px", "position:fixed;left:-2000px",
    "height:0;overflow:hidden", "width:0;overflow:hidden",
    "position:absolute;clip:rect(0px,0px,0px,0px)", "clip-path:inset(100%)",
])
def test_hidden_or_offscreen_challenges_do_not_pause(page, hidden):
    _, result = observe(page, f"""<main><article data-recruitops-application>
      <h3>AI应用岗</h3><p>申请成功</p></article></main>
      <section style='{hidden}'><div data-captcha>请完成安全验证 验证码</div></section>""")
    assert result["status"] == "SUCCEEDED"
    assert "auth_evidence" not in result["result"]


@pytest.mark.parametrize("attribute", ["hidden", "inert", "aria-hidden='true'"])
def test_ancestor_semantic_hiding_does_not_pause(page, attribute):
    _, result = observe(page, f"<p>我的投递记录</p><div {attribute}><div data-sitekey='fixture'>请完成安全验证</div></div>")
    assert result["status"] == "SUCCEEDED"


@pytest.mark.parametrize("html", [
    "<p>请勿向他人透露验证码</p>",
    "<p>验证码说明：可使用手机验证码登录。</p>",
    "<section style='opacity:0'><p>请完成安全验证</p></section><p>我的投递记录</p>",
])
def test_help_text_and_transparent_text_are_not_challenges(page, html):
    _, result = observe(page, html)
    assert result["status"] == "SUCCEEDED"


AUTH_FIXTURES = json.loads((Path(__file__).parents[1] / "extension/fixtures/authentication-gates.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("sample", AUTH_FIXTURES, ids=lambda sample: sample["id"])
def test_sanitized_real_login_wording_and_public_home_navigation(page, sample):
    controls = "<input type='tel'><input id='captcha' placeholder='验证码'>" if sample["expected"] == "LOGIN_REQUIRED" else ""
    _, result = observe(page, f"<main><p>{sample['text']}</p>{controls}</main>")
    if sample["expected"] == "SUCCEEDED":
        assert result["status"] == "SUCCEEDED"
        assert result["result"]["application_records"] == []
    else:
        assert result["error_code"] == "LOGIN_REQUIRED"
        assert result["result"]["auth_evidence"]["trigger"] == "visible_text"


@pytest.mark.parametrize("marker", ["data-captcha", "data-sitekey='placeholder'", "data-recruitops-auth='captcha'"])
def test_empty_preloaded_challenge_hosts_are_not_active_challenges(page, marker):
    _, result = observe(page, f"<main>我的投递记录</main><div {marker} style='width:400px;height:100px'></div>")
    assert result["status"] == "SUCCEEDED"


@pytest.mark.parametrize("challenge", [
    "<p>请完成滑块验证</p>", "<div data-captcha>请完成人机验证</div>",
    "<div data-sitekey='active'><canvas width='300' height='80'></canvas></div>",
    "<p>请输入图形验证码</p><input id='captcha'><img alt='验证码' width='80' height='30'>",
])
def test_active_human_challenges_keep_priority_over_sms_login(page, challenge):
    _, result = observe(page, f"<main>手机号登录 获取验证码<input type='tel'><input id='sms-code'></main>{challenge}")
    assert result["error_code"] == "CAPTCHA_REQUIRED"


def test_visible_challenge_pauses_with_redacted_bounded_evidence(page):
    raw, result = observe(page, """<section data-captcha>
      请完成安全验证 alice@example.test 13800138000 验证码:654321 token=private-secret
      https://example.test/challenge?session=private-url
      <input value='private-input'><textarea>private-textarea</textarea>
      </section>""")
    assert result["error_code"] == "CAPTCHA_REQUIRED"
    evidence = result["result"]["auth_evidence"]
    assert evidence == raw["auth_evidence"]
    assert evidence["trigger"] == "selector" and evidence["selector"] == "[data-captcha]"
    assert len(evidence["text"]) <= 240
    assert "请完成安全验证" in evidence["text"]
    for private in ("alice@", "13800138000", "654321", "private-secret", "private-url", "private-input", "private-textarea"):
        assert private not in json.dumps(result)
    assert "application_records" not in result["result"]


def test_adapter_redacts_untrusted_auth_evidence_again(page):
    raw, _ = observe(page, "<p>请完成安全验证</p>")
    raw["auth_evidence"]["text"] = "验证码:654321 token=private-secret alice@example.test"
    normalized = node_call("a.normalizeObservation(input.raw,input.context)", {"raw": raw, "context": CONTEXT})
    assert "654321" not in json.dumps(normalized)
    assert "private-secret" not in json.dumps(normalized)
    assert "alice@" not in json.dumps(normalized)
    raw["auth_evidence"]["selector"] = "input[value='private-secret']"
    with pytest.raises(subprocess.CalledProcessError):
        node_call("a.normalizeObservation(input.raw,input.context)", {"raw": raw, "context": CONTEXT})


def test_child_frame_pause_preserves_auth_evidence(page):
    top, _ = observe(page, "<p>投递查询</p>")
    child, child_result = observe(page, "<div data-captcha>请完成安全验证</div>")
    merged = node_call("a.normalizeFrameObservations(input.frames,input.context)", {
        "frames": [{"frameId": 0, "frameUrl": CONTEXT["page_url"], "raw": top},
                   {"frameId": 1, "frameUrl": "https://ats.example/challenge", "raw": child}],
        "context": CONTEXT,
    })
    assert merged["error_code"] == "CAPTCHA_REQUIRED"
    assert merged["result"]["auth_evidence"] == child_result["result"]["auth_evidence"]
