"""Entire-browser regression using only intercepted synthetic API responses."""

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright


WEB = Path(__file__).resolve().parents[2] / "apps" / "web"


@pytest.mark.parametrize("width", [1440, 390])
def test_application_browse_progress_and_human_mail_binding(width, tmp_path):
    calls, errors, approvals = [], [], []
    applications = [{"id": f"app-{index}", "company_name": f"合成公司{index:03}",
        "job_title": f"离线岗位{index:03}",
        "stage": "applied" if index < 82 else "written" if index < 85 else "interview1" if index == 85 else "rejected",
        "stage_history": [], "updated_at": "2026-09-24T00:00:00Z"} for index in range(106)]
    mail = {"id": "mail-fixture", "subject": "合成面试邀请", "sender": "fixture@example.test",
        "category": "interview", "confidence": None, "legacy_confidence": 0,
        "analysis_state": "analyzed", "binding_state": "pending", "association_required": True,
        "processing_status": "ambiguous_application", "processing_label": "待确认投递",
        "requires_confirmation": True, "content_digest": "a" * 64, "binding_revision": 0,
        "application_id": None, "received_at": "2026-09-24T00:00:00Z"}
    progress = {"runs": [
        {"run_id": "mail-run-fixture", "task_kind": "recruitment_mail", "status": "running",
         "phase": "analysis", "completed": 2, "total": 5, "failed": 1, "blocked": 0, "unit": "封"},
        {"run_id": "review-run-fixture", "task_kind": "application_review", "status": "running",
         "phase": "reviewing", "completed": 3, "total": 53, "failed": 0, "blocked": 2, "unit": "条"},
        {"run_id": "old-completed-fixture", "task_kind": "recruitment_mail", "status": "completed",
         "phase": "completed", "completed": 999, "total": 999, "unit": "封"},
    ]}
    configuration = {"settings": {"llm_enabled": False, "codex_runtime_enabled": False,
            "mail_enabled": False, "mail_imap_port": 993, "mail_imap_mailbox": "INBOX"},
        "profile": {"skills": [], "matching": {"title_keywords": []}, "scope": {"industry_groups": []}},
        "secrets": {}, "model_connections": [], "options": {"industry_groups": [], "mail_providers": []},
        "module_readiness": {"assistant": {"status": "disabled"}}, "onboarding": {"ready": True}}

    def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        assert url.netloc == "ui.example.test", "No real network or mail service permitted"
        calls.append((request.method, url.path, url.query, request.post_data))
        filename = url.path.lstrip("/") or "index.html"
        if filename in {"index.html", "app.js", "configuration.js", "company-sources.js", "styles.css"}:
            content_type = "text/html" if filename.endswith("html") else "text/css" if filename.endswith("css") else "application/javascript"
            return route.fulfill(body=(WEB / filename).read_text(encoding="utf-8"), content_type=content_type)
        if url.path == "/health":
            return route.fulfill(json={"status": "ok", "mode": "offline-fixture"})
        if url.path == "/api/local-ui/configuration/read":
            return route.fulfill(json=configuration)
        if url.path == "/api/codex/health":
            return route.fulfill(json={"enabled": False, "ready": False, "state": "disabled"})
        if url.path == "/api/local-ui/tasks/progress":
            return route.fulfill(json=progress)
        if url.path == "/api/applications/page":
            query = parse_qs(url.query)
            term = query.get("query", [""])[0]
            stages = query.get("stages", [])
            matching = [item for item in applications if term in item["company_name"] + item["job_title"]]
            filtered = [item for item in matching if not stages or item["stage"] in stages]
            stage_counts = {stage: sum(item["stage"] == stage for item in matching)
                            for stage in {item["stage"] for item in matching}}
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["50"])[0])
            return route.fulfill(json={"items": filtered[offset:offset + limit], "total": len(filtered),
                                       "unfiltered_total": len(applications), "stage_counts": stage_counts})
        if url.path == "/api/jobs/browse":
            return route.fulfill(json={"items": [], "featured": [], "total": 0,
                "stats": {}, "facets": {"companies": [], "categories": {}, "platforms": []}})
        if url.path == "/api/recruitment-mails":
            assert parse_qs(url.query).get("refresh") == ["false"]
            return route.fulfill(json={"items": [mail], "total": 1, "freshness": {"status": "cached"}})
        if url.path == "/api/recruitment-mails/mail-fixture/binding-candidates":
            return route.fulfill(json={"record_id": mail["id"], "content_digest": mail["content_digest"],
                "binding_revision": mail["binding_revision"], "current_application_id": mail["application_id"],
                "requires_user_confirmation": True, "candidates": [{"application_id": "app-52",
                    "company_name": "合成公司052", "job_title": "离线岗位052", "reason": "manual_selection"}], "total": 1})
        if url.path == "/api/recruitment-mails/mail-fixture/binding-proposals":
            assert request.method == "POST" and request.headers["x-recruitops-local-ui"] == "1"
            body = json.loads(request.post_data)
            assert body == {"record_id": "mail-fixture", "application_id": "app-52", "action": "bind",
                            "content_digest": "a" * 64, "binding_revision": 0}
            preview = {"before": {"subject": mail["subject"], "sender": mail["sender"]},
                       "after": {"action": "bind", "company_name": "合成公司052", "job_title": "离线岗位052"}}
            approvals.append({"token_id": "approval-fixture", "status": "pending",
                              "operation": "recruitment_mail_binding", "preview": preview})
            return route.fulfill(json={"success": True, "data": {"approval_id": "approval-fixture",
                "approval_status": "pending", "business_write_performed": False, "preview": preview}})
        if url.path == "/api/approvals/approval-fixture/approve":
            assert request.method == "POST" and request.headers["x-recruitops-local-ui"] == "1"
            assert approvals[0]["status"] == "pending"
            approvals[0]["status"] = "approved"
            return route.fulfill(json={"allowed": True, "status": "approved"})
        if url.path == "/api/approvals/approval-fixture/execute":
            assert request.method == "POST" and request.headers["x-recruitops-local-ui"] == "1"
            assert approvals[0]["status"] == "approved"
            assert json.loads(request.post_data) == {"operator": "local-ui-user"}
            approvals[0]["status"] = "consumed"
            mail.update(application_id="app-52", binding_state="confirmed", binding_revision=1)
            return route.fulfill(json={"success": True})
        if url.path == "/api/approvals":
            return route.fulfill(json=approvals)
        if url.path in {"/api/schedule", "/api/companies", "/api/codex/traces", "/api/automations"}:
            return route.fulfill(json=[])
        if url.path == "/api/reports/operational":
            return route.fulfill(json={"counts": {}})
        return route.fulfill(status=503, json={"detail": "Offline fixture only"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.environ.get("RECRUITOPS_TEST_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": width, "height": 1000}, service_workers="block")
        context.route("**/*", route_request)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://ui.example.test/", wait_until="networkidle")

        def navigate(view):
            if width < 700:
                page.locator("#mobile-menu-button").click()
            page.locator(f'#sidebar [data-view="{view}"]').click()

        navigate("applications")
        expect(page.locator("#application-stage-filter")).to_have_count(0)
        expect(page.locator(".application-card")).to_have_count(74)
        expect(page.locator("#application-page-description")).to_have_text("共 106 条 · 已显示 74 条")
        expect(page.locator(".kanban-column--written .application-card")).to_have_count(3)
        expect(page.locator(".kanban-column--interview .application-card")).to_have_count(1)
        expect(page.locator(".kanban-column--closed .application-card")).to_have_count(20)
        page.locator('[data-application-more="applied"]').click()
        expect(page.locator(".application-card")).to_have_count(106)
        expect(page.locator("#application-page-description")).to_have_text("共 106 条 · 已显示 106 条")
        expect(page.locator('[data-application-more="applied"]')).to_have_count(0)
        page.locator("#application-search").fill("离线岗位052")
        expect(page.locator(".application-card")).to_have_count(1)
        expect(page.locator("#application-page-description")).to_have_text("匹配 1 条 · 已显示 1 条")
        expect(page.locator("#nav-application-count")).to_have_text("106")
        expect(page.locator("#application-kanban")).to_contain_text("合成公司052")
        page.locator("#application-search").fill("没有此岗位")
        expect(page.locator("#application-kanban")).to_contain_text("没有匹配的投递记录")
        page.locator('#application-filter-form button[type="reset"]').click()
        expect(page.locator(".application-card")).to_have_count(74)
        expect(page.locator(".kanban-column--written .application-card")).to_have_count(3)
        expect(page.locator(".kanban-column--interview .application-card")).to_have_count(1)
        expect(page.locator(".kanban-column--closed .application-card")).to_have_count(20)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        application_capture = tmp_path / f"applications-{width}.png"
        page.screenshot(path=str(application_capture), animations="disabled")

        navigate("assistant")
        expect(page.locator("#assistant-task-progress-title")).to_contain_text("处理招聘邮件")
        expect(page.locator("#assistant-task-progress-detail")).to_contain_text("已处理 2 / 5 封")
        expect(page.locator("#assistant-more-task-progress")).to_contain_text("官网投递状态复核")
        expect(page.locator("#assistant-more-task-progress")).to_contain_text("已处理 3 / 53 条")
        expect(page.locator("#assistant-more-task-progress .assistant-task-progress")).to_have_count(1)
        expect(page.locator("#assistant-view")).not_to_contain_text("999")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        progress_capture = tmp_path / f"task-progress-{width}.png"
        page.screenshot(path=str(progress_capture), animations="disabled")
        progress["runs"] = [progress["runs"][2]]
        navigate("applications")
        navigate("assistant")
        expect(page.locator("#assistant-task-progress")).to_be_hidden()
        expect(page.locator("#assistant-more-task-progress .assistant-task-progress")).to_have_count(0)

        navigate("mail")
        expect(page.locator("#mail-list")).to_contain_text("待确认投递")
        expect(page.locator("#mail-list")).not_to_contain_text("0%")
        page.get_by_role("button", name="关联投递", exact=True).click()
        expect(page.locator("#job-detail-dialog")).to_be_visible()
        page.get_by_role("button", name="选择并查看确认预览", exact=True).click()
        dialog = page.locator("#job-detail-dialog")
        expect(dialog).to_contain_text("邮件：合成面试邀请")
        expect(dialog).to_contain_text("发件人：fixture@example.test")
        expect(dialog).to_contain_text("关联到：合成公司052 · 离线岗位052")
        expect(dialog).to_contain_text("不会直接更改投递阶段")
        assert not any(path.endswith(("/approve", "/execute")) for _, path, _, _ in calls)
        assert mail["application_id"] is None
        binding_capture = tmp_path / f"mail-binding-{width}.png"
        page.screenshot(path=str(binding_capture), animations="disabled")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        dialog.get_by_role("button", name="确认关联", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page.locator("#mail-list")).to_contain_text("用户已确认")
        read_posts = {"/api/local-ui/configuration/read", "/api/local-ui/configuration/latest-crawl"}
        actions = [path for method, path, _, _ in calls if method == "POST" and path not in read_posts]
        assert actions == ["/api/recruitment-mails/mail-fixture/binding-proposals",
                           "/api/approvals/approval-fixture/approve", "/api/approvals/approval-fixture/execute"]
        assert not any("/sync" in path or "/process" in path or "/turns" in path for _, path, _, _ in calls)
        assert not errors
        print("UI fixture screenshots:", application_capture.resolve(), progress_capture.resolve(), binding_capture.resolve())
        browser.close()
