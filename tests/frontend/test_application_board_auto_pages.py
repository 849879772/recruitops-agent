"""Offline browser regression: the board must not stop at its first 50 rows."""

import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


WEB = Path(__file__).resolve().parents[2] / "apps" / "web"


def test_application_board_loads_all_pages_and_search_restores_all_records():
    records = [
        {"id": f"applied-{index}", "company_name": "Example company",
         "job_title": f"Engineer {index}", "stage": "applied",
         "updated_at": "2026-09-26T00:00:00Z", "stage_history": [],
         "record_url": "https://careers.example.test/progress" if index % 2 == 0 else None}
        for index in range(122)
    ] + [
        {"id": "written-1", "company_name": "Written company",
         "job_title": "Test engineer", "stage": "written", "stage_history": []},
        {"id": "interview-1", "company_name": "Interview company",
         "job_title": "Interview engineer", "stage": "interview1", "stage_history": []},
    ]
    calls, errors, writes = [], [], []
    failure = {"enabled": False}

    def route_request(route):
        url = urlsplit(route.request.url)
        assert url.netloc == "ui.example.test", "All network access must remain mocked"
        filename = url.path.lstrip("/") or "index.html"
        if filename in {"index.html", "app.js", "configuration.js", "company-sources.js", "styles.css"}:
            mime = "text/html" if filename.endswith("html") else "text/css" if filename.endswith("css") else "application/javascript"
            return route.fulfill(body=(WEB / filename).read_text(encoding="utf-8"), content_type=mime)
        if url.path == "/health":
            return route.fulfill(json={"status": "ok", "mode": "read_only"})
        if url.path == "/api/codex/health":
            return route.fulfill(json={"enabled": False, "ready": False})
        if url.path == "/api/local-ui/applications/applied-121/record-url":
            assert route.request.method == "PATCH"
            body = route.request.post_data_json
            writes.append(body)
            assert set(body) == {"record_url", "expected_updated_at"}
            record = next(row for row in records if row["id"] == "applied-121")
            record["record_url"] = body["record_url"]
            record["updated_at"] = "2026-09-27T00:00:00Z"
            return route.fulfill(json={"status": "updated"})
        if url.path == "/api/applications/page":
            params = parse_qs(url.query)
            query = params.get("query", [""])[0].casefold()
            matches = [row for row in records if query in f"{row['company_name']} {row['job_title']}".casefold()]
            counts = {}
            for row in matches:
                counts[row["stage"]] = counts.get(row["stage"], 0) + 1
            rows = [row for row in matches if row["stage"] in params.get("stages", [])]
            offset, limit = int(params.get("offset", [0])[0]), int(params.get("limit", [50])[0])
            calls.append(params)
            if failure["enabled"] and "applied" in params.get("stages", []) and offset == 50:
                return route.fulfill(status=503, json={"detail": "Synthetic next-page failure"})
            return route.fulfill(json={
                "items": rows[offset:offset + limit], "total": len(rows),
                "unfiltered_total": len(records), "stage_counts": counts,
                "limit": limit, "offset": offset,
            })
        if url.path in {"/api/schedule", "/api/companies", "/api/approvals", "/api/codex/traces"}:
            return route.fulfill(json=[])
        return route.fulfill(status=503, json={"detail": "Offline fixture: unavailable"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.environ.get("RECRUITOPS_TEST_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        context.route("**/*", route_request)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://ui.example.test/", wait_until="networkidle")
        page.locator('[data-view="applications"]').click()
        applied = page.locator(".kanban-column--applied .application-card")
        expect(applied).to_have_count(122)
        expect(page.locator(".application-card")).to_have_count(124)
        expect(page.locator("#application-page-description")).to_have_text("共 124 条 · 已显示 124 条")
        expect(applied.last).to_contain_text("Engineer 121")
        expect(page.locator("[data-application-more]")).to_have_count(0)
        assert {int(call["offset"][0]) for call in calls if "applied" in call["stages"]} >= {0, 50, 100}

        channels = page.locator("#application-channel-filter")
        expect(channels).to_have_text("全部 124可官网复核 61仅邮件更新 63")
        request_count = len(calls)
        channels.locator('[data-application-channel="mail_only"]').click()
        expect(page.locator(".application-card")).to_have_count(63)
        expect(applied).to_have_count(61)
        expect(applied.last).to_contain_text("没有官网进度链接，不参与官网复核，通过邮件更新")
        assert len(calls) == request_count

        page.locator("#application-search").fill("Engineer 121")
        expect(applied).to_have_count(1)
        expect(applied).to_contain_text("Engineer 121")
        channels.locator('[data-application-channel="official_page"]').click()
        expect(page.locator(".application-card")).to_have_count(0)
        expect(page.locator("#application-kanban")).to_contain_text("当前分类暂无记录")
        channels.locator('[data-application-channel="mail_only"]').click()
        expect(applied).to_have_count(1)
        page.locator("#application-search").fill("")
        expect(applied).to_have_count(61)
        channels.locator('[data-application-channel="all"]').click()
        expect(applied).to_have_count(122)
        expect(page.locator(".kanban-column--written .application-card")).to_have_count(1)
        expect(page.locator(".kanban-column--interview .application-card")).to_have_count(1)

        failure["enabled"] = True
        page.locator("#applications-refresh-button").click()
        expect(page.locator("#applications-refresh-button")).to_be_enabled()
        # A failed background refresh must retain the previously loaded cards.
        expect(applied).to_have_count(122)
        expect(page.locator(".kanban-column--written .application-card")).to_have_count(1)
        channels.locator('[data-application-channel="mail_only"]').click()
        expect(applied).to_have_count(61)
        expect(page.locator("#application-page-description")).to_have_text("仅邮件更新 · 已加载 63 条（加载未完成）")
        expect(page.locator("#application-channel-count-note")).to_contain_text("已加载")
        expect(page.locator(".application-summary-item--applied strong")).to_have_text("61")
        retry = page.locator('[data-application-more="applied"]')
        expect(retry).to_be_visible()
        expect(retry).to_be_enabled()
        failure["enabled"] = False
        retry.click()
        expect(applied).to_have_count(61)
        expect(channels).to_have_text("全部 124可官网复核 61仅邮件更新 63")

        page.locator('[data-application-add-link="applied-121"]').click()
        editor = page.locator("#application-edit-dialog .application-editor")
        expect(editor).to_be_visible()
        expect(editor.locator('input[type="url"]')).to_be_focused()
        editor.locator('input[type="url"]').fill("https://careers.example.test/my/applications")
        editor.get_by_role("button", name="保存进度页链接", exact=True).click()
        expect(applied).to_have_count(60)
        expect(channels).to_have_text("全部 124可官网复核 62仅邮件更新 62")
        assert len(writes) == 1
        assert records[121]["stage"] == "applied"
        channels.locator('[data-application-channel="official_page"]').click()
        expect(applied.filter(has_text="Engineer 121")).to_have_count(1)
        expect(applied.filter(has_text="Engineer 121").get_by_role("link", name="查看投递进度 ›")).to_have_attribute("href", "https://careers.example.test/my/applications")
        channels.locator('[data-application-channel="all"]').click()
        expect(applied).to_have_count(122)
        expect(page.locator("#application-page-description")).to_have_text("共 124 条 · 已显示 124 条")
        assert not errors
        browser.close()
