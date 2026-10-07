"""Real browser coverage with synthetic routes; never contact recruitment sites."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright


WEB = Path(__file__).resolve().parents[2] / "apps" / "web"


@pytest.mark.parametrize("width", [1440, 390])
def test_identity_queue_one_click_and_dismiss_without_reopening(width, tmp_path):
    writes, errors = [], []
    saved = {"value": False}
    item = {"application_id": "fixture-application", "company_name": "示例企业", "job_title": "本地平台开发工程师",
            "identity_digest": "d" * 64, "binding_revision": 0, "operation_id": "fixture-observation",
            "captured_at": "2026-09-28T12:00:00Z", "candidates": [
                {"candidate_id": "a" * 64, "raw_title": "官网平台开发工程师（示例方向）", "selectable": True,
                 "context": "当前进度：筛选", "external_job_id": "EXAMPLE-A"},
                {"candidate_id": "b" * 64, "raw_title": "本地平台开发工程师-深圳第 2 志愿", "selectable": True},
                {"candidate_id": "c" * 64, "raw_title": "另一官网岗位", "selectable": False}]}

    def route_request(route):
        url = urlsplit(route.request.url)
        assert url.netloc == "ui.example.test", "All network access must be intercepted"
        filename = url.path.lstrip("/") or "index.html"
        if filename in {"index.html", "app.js", "configuration.js", "company-sources.js", "styles.css"}:
            mime = "text/html" if filename.endswith("html") else "text/css" if filename.endswith("css") else "application/javascript"
            return route.fulfill(body=(WEB / filename).read_text(encoding="utf-8"), content_type=mime)
        if route.request.method != "GET" and url.path.startswith(("/api/applications/", "/api/approvals/")):
            writes.append((url.path, route.request.post_data_json))
        if url.path == "/health":
            return route.fulfill(json={"status": "ok", "mode": "read_only"})
        if url.path == "/api/codex/health":
            return route.fulfill(json={"enabled": False, "ready": False})
        if url.path == "/api/applications/page":
            return route.fulfill(json={"items": [], "total": 0, "unfiltered_total": 0, "stage_counts": {}})
        if url.path == "/api/applications/identity-queue":
            return route.fulfill(json={"items": [] if saved["value"] else [item], "total": 0 if saved["value"] else 1, "read_only": True})
        if url.path.endswith("/identity-proposals"):
            assert route.request.post_data_json["candidate_id"] == "b" * 64
            return route.fulfill(json={"success": True, "data": {"approval_id": "fixture-token", "approval_status": "pending"}})
        if url.path.endswith("/approve"):
            return route.fulfill(json={"allowed": True, "status": "approved"})
        if url.path.endswith("/execute"):
            saved["value"] = True
            return route.fulfill(json={"success": True})
        if url.path in {"/api/approvals", "/api/schedule", "/api/companies", "/api/codex/traces"}:
            return route.fulfill(json=[])
        return route.fulfill(status=503, json={"detail": "Synthetic offline fixture"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.environ.get("RECRUITOPS_TEST_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": width, "height": 900}, service_workers="block")
        context.route("**/*", route_request)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://ui.example.test/", wait_until="networkidle")
        if width < 700:
            page.locator("#mobile-menu-button").click()
        page.locator('[data-view="applications"]').click()
        entry = page.locator("#application-identity-queue-button")
        expect(entry).to_have_text("待核对 1 项")
        panel = page.locator("#application-identity-queue-panel")
        expect(panel).to_be_hidden()
        entry.click()
        expect(panel).to_be_visible()
        expect(panel.locator(".identity-queue-card")).to_have_count(1)
        expect(panel).to_contain_text("本地平台开发工程师")
        expect(panel).to_contain_text("官网平台开发工程师（示例方向）")
        expect(panel.locator("input").last).to_be_disabled()
        expect(panel.locator("input:checked")).to_have_count(0)
        expect(panel.get_by_role("button", name="确认对应", exact=True)).to_be_disabled()
        page.screenshot(path=str(tmp_path / f"identity-queue-{width}.png"))
        assert panel.bounding_box()["width"] <= width
        page.locator("#application-identity-queue-close").click()
        page.locator("#applications-refresh-button").click()
        expect(panel).to_be_hidden()
        entry.click()
        expect(panel.locator("input:checked")).to_have_count(0)
        panel.locator("input").nth(1).check()
        expect(panel).to_contain_text("对应到“本地平台开发工程师-深圳第 2 志愿”")
        assert writes == [], "Choosing a candidate is not write authorization"
        panel.get_by_role("button", name="确认对应", exact=True).click()
        expect(entry).to_have_text("待核对 0 项")
        expect(panel.locator("input")).to_have_count(0)
        assert [path for path, _ in writes] == [
            "/api/applications/fixture-application/identity-proposals",
            "/api/approvals/fixture-token/approve", "/api/approvals/fixture-token/execute"]
        page.reload(wait_until="networkidle")
        expect(entry).to_have_text("待核对 0 项")
        expect(panel).to_be_hidden()
        assert not errors
        browser.close()


@pytest.mark.parametrize("reason,message", [
    ("application_records_missing", "尚未提取到可选择的官网岗位"),
    ("observation_expired; review_the_application_again", "页面证据已过期"),
    ("observation_not_found; review_the_application_first", "尚无可用的官网页面证据"),
])
def test_empty_candidates_explain_reason_and_reread_requires_a_click(reason, message):
    writes, errors = [], []
    state = {"read": False, "fail": True}
    item = {"application_id": "fixture-application", "company_name": "示例企业", "job_title": "AI 开发工程师",
            "identity_digest": "d" * 64, "binding_revision": 0, "operation_id": "old-observation",
            "unavailable_reason": reason, "candidates": []}
    choice = {"candidate_id": "a" * 64, "raw_title": "AI应用开发工程师", "selectable": True,
              "context": "投递简历 2026-09-29", "evidence_source": "vision"}

    def route_request(route):
        url = urlsplit(route.request.url)
        assert url.netloc == "ui.example.test"
        name = url.path.lstrip("/") or "index.html"
        if name in {"index.html", "app.js", "configuration.js", "company-sources.js", "styles.css"}:
            mime = "text/html" if name.endswith("html") else "text/css" if name.endswith("css") else "application/javascript"
            return route.fulfill(body=(WEB / name).read_text(encoding="utf-8"), content_type=mime)
        if url.path == "/health":
            return route.fulfill(json={"status": "ok", "mode": "read_only"})
        if url.path == "/api/codex/health":
            return route.fulfill(json={"enabled": False, "ready": False})
        if url.path == "/api/applications/page":
            return route.fulfill(json={"items": [], "total": 0, "unfiltered_total": 0, "stage_counts": {}})
        if url.path == "/api/applications/identity-queue":
            current = {**item, "candidates": [choice], "operation_id": "new-observation"} if state["read"] else item
            return route.fulfill(json={"items": [current], "total": 1})
        if route.request.method == "POST" and url.path.startswith(("/api/applications/", "/api/approvals/")):
            assert url.path == "/api/applications/fixture-application/identity-reread", "Reread must not approve or write a stage"
            assert set(route.request.post_data_json) == {"request_id"}
            writes.append(route.request.post_data_json)
            if state["fail"]:
                return route.fulfill(status=409, json={"detail": "请先登录官网，投递阶段未修改"})
            state["read"] = True
            return route.fulfill(json={"candidates": [choice], "stage_unchanged": True})
        if url.path in {"/api/approvals", "/api/schedule", "/api/companies", "/api/codex/traces"}:
            return route.fulfill(json=[])
        return route.fulfill(status=503, json={"detail": "Offline fixture"})

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        context.route("**/*", route_request)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://ui.example.test/", wait_until="networkidle")
        page.locator('[data-view="applications"]').click()
        page.locator("#application-identity-queue-button").click()
        panel = page.locator("#application-identity-queue-panel")
        expect(panel).to_contain_text(message)
        expect(panel.get_by_role("button", name="确认对应", exact=True)).to_have_count(0)
        assert writes == []
        reread = panel.get_by_role("button", name="重新读取该岗位", exact=True)
        reread.click()
        expect(panel).to_contain_text("请先登录官网，投递阶段未修改")
        expect(reread).to_be_enabled()
        state["fail"] = False
        reread.click()
        expect(panel.locator('input[type="radio"]')).to_have_count(1)
        expect(panel).to_contain_text("来自本次官网截图识别")
        expect(panel.get_by_role("button", name="确认对应", exact=True)).to_be_disabled()
        panel.locator('input[type="radio"]').check()
        expect(panel.get_by_role("button", name="确认对应", exact=True)).to_be_enabled()
        assert len(writes) == 2 and not errors
        browser.close()
