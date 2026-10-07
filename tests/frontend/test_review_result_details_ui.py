"""Actual Chromium, synthetic receipts only; never access official data or sites."""

import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright


WEB = Path(__file__).resolve().parents[2] / "apps" / "web"
RUN = "status-review-" + "a" * 32


def receipt(index, *, state="failed", reason="desktop_readiness_timeout", **extra):
    return {"application_id": f"fixture-{index}", "company_name": f"离线公司{index}",
        "job_title": f"合成岗位{index}", "state": state, "reason": reason,
        "saved_stage": "applied", "presentation_state": None, "name_source": "review_result",
        "record_url": f"https://careers.example.test/applications/{index}",
        "checked_at": "2026-09-29T10:00:00Z", "model_disposition": "called",
        "vision_disposition": "not_requested", **extra}


class ReviewResultsFixture:
    def __init__(self):
        self.calls = []
        self.errors = []
        self.expired = False
        self.fail_next_page = False
        self.rechecks = []
        self.rechecked_run = None
        self.finished = []
        self.attention = [receipt(0), receipt(1, state="blocked", reason="login_required"),
            receipt(2, state="unresolved", reason="model_invalid_output"),
            receipt(3, reason="review_batch_busy"), receipt(4)]
        self.retained = [receipt("retained", state="unresolved", reason="record_present_status_unknown",
                                 presentation_state="retained", saved_stage="written")]
        self.latest = [receipt("latest", company_name="跨任务最新公司", job_title="跨任务最新岗位")]

    def route(self, route):
        url = urlsplit(route.request.url)
        assert url.netloc == "ui.example.test", "Every request is mocked; real network access is forbidden"
        filename = url.path.lstrip("/") or "index.html"
        if url.path == "/api/applications/review-results/recheck":
            body = route.request.post_data_json
            self.rechecks.append(body)
            self.rechecked_run = "status-review-" + "b" * 32
            self.finished = [{**item, "state":"unchanged", "reason":"verified"} for item in self.attention
                             if item["application_id"] in body.get("application_ids", [])]
            return route.fulfill(json={"run_id":self.rechecked_run, "scope_complete":True,
                "continuation_required":False, "summary":{"scope_complete":True}, "message":"本组复核已结束"})
        if filename in {"index.html", "app.js", "configuration.js", "company-sources.js", "styles.css"}:
            source = (WEB / filename).read_text(encoding="utf-8")
            if filename == "app.js":
                source = source.replace("if (globalThis.__RECRUITOPS_TEST_MODE__) {",
                    "globalThis.reviewResultsHooks = testHooks; if (globalThis.__RECRUITOPS_TEST_MODE__) {")
            mime = "text/html" if filename.endswith("html") else "text/css" if filename.endswith("css") else "application/javascript"
            return route.fulfill(body=source, content_type=mime)
        if url.path == "/api/applications/review-results":
            assert route.request.method == "GET", "Opening result details must never mutate data"
            query = parse_qs(url.query)
            self.calls.append(query)
            scope = query.get("scope", ["run"])[0]
            category = query.get("category", ["attention"])[0]
            if scope == "latest":
                assert "run_id" not in query, "Latest receipts are never a historical run"
                items = self.latest
            elif query.get("run_id") == [self.rechecked_run] and self.rechecked_run:
                items = self.finished
            elif self.expired:
                return route.fulfill(json={"scope": "run", "run_id": RUN, "run_status": "completed",
                    "details_expired": True, "items": [], "total": None, "has_more": False,
                    "next_cursor": None, "summary": {"total": 179}})
            else:
                items = self.attention if category == "attention" else self.retained if category == "retained" else (
                    self.attention + self.retained if category == "all" else [item for item in self.attention if item["state"] == category])
            if self.fail_next_page and query.get("cursor"):
                self.fail_next_page = False
                return route.fulfill(status=503, json={"detail": "合成分页暂不可用"})
            # A short server-side character budget can yield fewer items than limit.
            offset = int(query.get("cursor", ["0"])[0])
            page = items[offset:offset + 2]
            next_offset = offset + len(page)
            has_more = next_offset < len(items)
            return route.fulfill(json={"scope": scope, "run_id": query.get("run_id", [RUN])[0] if scope == "run" else None,
                "run_status": "completed", "category": category, "details_expired": False,
                "items": page, "total": len(items), "has_more": has_more,
                "next_cursor": str(next_offset) if has_more else None, "summary": {"total": 179}})
        if url.path == "/api/applications/page":
            return route.fulfill(json={"items": [], "total": 0, "unfiltered_total": 0, "stage_counts": {}})
        if url.path == "/api/applications/identity-queue":
            return route.fulfill(json={"items": [], "total": 0, "read_only": True})
        if url.path == "/health":
            return route.fulfill(json={"status": "ok", "mode": "read_only"})
        if url.path == "/api/codex/health":
            return route.fulfill(json={"enabled": False, "ready": False})
        if url.path in {"/api/approvals", "/api/schedule", "/api/companies", "/api/codex/traces"}:
            return route.fulfill(json=[])
        return route.fulfill(status=503, json={"detail": "Offline synthetic fixture"})


@pytest.fixture
def ui():
    fixture = ReviewResultsFixture()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get("RECRUITOPS_TEST_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
        context.route("**/*", fixture.route)
        page = context.new_page()
        page.on("pageerror", lambda error: fixture.errors.append(str(error)))
        page.goto("https://ui.example.test/", wait_until="networkidle")
        try:
            yield page, fixture
            assert not fixture.errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_header_opens_attention_names_reasons_and_safe_progress_links(ui, width):
    page, fixture = ui
    page.set_viewport_size({"width": width, "height": 1000})
    if width < 700:
        page.locator("#mobile-menu-button").click()
    page.locator('[data-view="applications"]').click()
    page.locator("#application-review-results-button").click()
    dialog = page.locator("#review-results-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator(".review-result-row")).to_have_count(2)
    first = dialog.locator(".review-result-row:not(.review-login-group)").first
    expect(first).to_contain_text("离线公司0 · 合成岗位0")
    expect(first).to_contain_text("执行失败")
    expect(first).to_contain_text("投递页面未在时限内就绪")
    login = dialog.locator(".review-login-group")
    expect(login).to_contain_text("离线公司1")
    expect(login).to_contain_text("合成岗位1")
    expect(login).to_contain_text("官网明确要求登录")
    expect(login.get_by_role("link", name="打开官网登录")).to_have_count(1)
    expect(first.get_by_role("link")).to_have_attribute("href", "https://careers.example.test/applications/0")
    expect(first.get_by_role("link")).to_have_attribute("rel", "noopener noreferrer")
    assert fixture.calls[0]["category"] == ["attention"]
    assert "run_id" not in fixture.calls[0]
    assert "undefined" not in dialog.inner_text()
    page.locator("#review-results-close").click()
    expect(dialog).not_to_be_visible()


def test_pagination_appends_every_item_then_filter_resets_cursor(ui):
    page, fixture = ui
    page.evaluate("reviewResultsHooks.openReviewResults()")
    dialog = page.locator("#review-results-dialog")
    rows = dialog.locator(".review-result-row")
    expect(rows).to_have_count(2)
    dialog.get_by_role("button", name="加载更多", exact=True).click()
    expect(rows).to_have_count(4)
    expect(rows.nth(3)).to_contain_text("review_batch_busy")  # unknown reasons remain visible
    dialog.get_by_role("button", name="加载更多", exact=True).click()
    expect(rows).to_have_count(5)
    expect(dialog.get_by_role("button", name="加载更多", exact=True)).to_be_hidden()
    assert {row.split(" · ")[0] for row in rows.locator("strong").all_text_contents()} == {f"离线公司{i}" for i in range(5)}
    assert fixture.calls[1]["run_id"] == [RUN]
    assert [query.get("cursor") for query in fixture.calls] == [None, ["2"], ["4"]]
    dialog.get_by_label("筛选复核结果").select_option("retained")
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text("离线公司retained")
    expect(rows).to_contain_text("保留原阶段：笔试")
    assert fixture.calls[-1]["category"] == ["retained"] and "cursor" not in fixture.calls[-1]
    dialog.get_by_label("筛选复核结果").select_option("all")
    expect(rows).to_have_count(2)
    assert fixture.calls[-1]["category"] == ["all"] and "cursor" not in fixture.calls[-1]


def test_expired_history_only_shows_latest_after_explicit_scope_selection(ui):
    page, fixture = ui
    fixture.expired = True
    page.evaluate("run => reviewResultsHooks.openReviewResults(run)", RUN)
    dialog = page.locator("#review-results-dialog")
    expect(dialog).to_contain_text("该轮详细诊断已按保留期清理")
    expect(dialog.locator(".review-result-row")).to_have_count(0)
    expect(dialog.get_by_role("button", name="加载更多", exact=True)).to_be_hidden()
    assert fixture.calls[-1]["run_id"] == [RUN]
    assert all(call["scope"] == ["run"] for call in fixture.calls)
    dialog.get_by_label("复核明细范围").select_option("latest")
    expect(dialog.locator(".review-result-row")).to_have_count(1)
    expect(dialog).to_contain_text("各投递最近一次结果，可能来自不同任务")
    expect(dialog).to_contain_text("跨任务最新公司 · 跨任务最新岗位")
    assert "run_id" not in fixture.calls[-1] and "cursor" not in fixture.calls[-1]
    dialog.get_by_label("复核明细范围").select_option("run")
    expect(dialog.locator(".review-result-row")).to_have_count(0)
    expect(dialog).not_to_contain_text("跨任务最新公司")
    assert fixture.calls[-1]["run_id"] == [RUN]


def test_dangerous_urls_are_never_rendered_and_text_is_not_html(ui):
    page, fixture = ui
    bad_urls = ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                "file:///C:/Windows/system.ini", "//evil.example.test/", "vbscript:msgbox(1)"]
    fixture.attention = [receipt(i, record_url=url) for i, url in enumerate(bad_urls)]
    fixture.attention.append(receipt("safe", company_name='<img src="x" onerror="window.injected=true">'))
    page.evaluate("reviewResultsHooks.openReviewResults()")
    dialog = page.locator("#review-results-dialog")
    for count in (4, 6):
        dialog.get_by_role("button", name="加载更多", exact=True).click()
        expect(dialog.locator(".review-result-row")).to_have_count(count)
    links = dialog.locator(".review-result-row a")
    expect(links).to_have_count(1)
    expect(links).to_have_attribute("href", "https://careers.example.test/applications/safe")
    expect(dialog.locator("img")).to_have_count(0)
    expect(dialog.locator(".review-result-row").last).to_contain_text('<img src="x"')
    assert page.evaluate("globalThis.injected === undefined")


def test_failed_next_page_preserves_existing_results_and_can_retry(ui):
    page, fixture = ui
    page.evaluate("reviewResultsHooks.openReviewResults()")
    fixture.fail_next_page = True
    dialog = page.locator("#review-results-dialog")
    dialog.get_by_role("button", name="加载更多", exact=True).click()
    expect(page.locator("#toast-region")).to_contain_text("已显示的记录保留")
    expect(dialog.locator(".review-result-row")).to_have_count(2)
    expect(dialog.get_by_role("button", name="加载更多", exact=True)).to_be_enabled()
    dialog.get_by_role("button", name="加载更多", exact=True).click()
    expect(dialog.locator(".review-result-row")).to_have_count(4)
    assert fixture.calls[-1]["cursor"] == fixture.calls[-2]["cursor"] == ["2"]


@pytest.mark.parametrize("width", [1440, 390])
def test_login_group_real_dom_lists_every_loaded_job_and_rechecks_only_on_click(ui, width):
    page, fixture = ui
    fixture.attention = [receipt(0, state="blocked", reason="login_required", company_name="同公司"),
        receipt(1, state="blocked", reason="captcha_required", company_name="同公司")]
    page.set_viewport_size({"width":width,"height":1000})
    page.evaluate("reviewResultsHooks.openReviewResults()")
    dialog = page.locator("#review-results-dialog")
    group = dialog.locator(".review-login-group")
    expect(group).to_have_count(1)
    expect(group.locator("li")).to_have_count(2)
    expect(group).to_contain_text("合成岗位0")
    expect(group).to_contain_text("合成岗位1")
    expect(group.get_by_role("link", name="打开官网登录")).to_have_count(1)
    assert fixture.rechecks == []
    box = group.bounding_box()
    assert box["x"] >= 0 and box["x"]+box["width"] <= width
    group.get_by_role("button", name="登录后重新复核此公司").click()
    expect(dialog).to_contain_text("同公司 · 合成岗位0")
    expect(dialog.locator(".review-login-group")).to_have_count(0)
    assert len(fixture.rechecks) == 1
    assert fixture.rechecks[0]["application_ids"] == ["fixture-0","fixture-1"]
    assert len(fixture.rechecks[0]["request_id"]) == 36
    assert fixture.calls[-1]["run_id"] == [fixture.rechecked_run]
    assert fixture.calls[-1]["category"] == ["all"]
