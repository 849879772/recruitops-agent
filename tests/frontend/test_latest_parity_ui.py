"""Browser regression with synthetic fixtures; every request is intercepted."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect


WEB = Path(__file__).resolve().parents[2] / "apps" / "web"


@pytest.mark.parametrize("width", [1440, 390])
def test_assistant_diagnostics_and_model_only_save(width, tmp_path):
    calls, errors = [], []
    config = {
        "settings": {"llm_enabled": False, "codex_runtime_enabled": False,
            "job_analysis_enabled": False, "mail_enabled": False, "mail_imap_host": "",
            "mail_imap_username": "", "mail_imap_port": 993, "mail_imap_mailbox": "INBOX"},
        "secrets": {"mail_imap_password": False},
        "profile": {"degree": None, "skills": [], "matching": {"title_keywords": []}, "scope": {"industry_groups": []}},
        "model_connections": [{"id": "fixture", "name": "Fixture", "provider": "openai-compatible",
            "api_style": "openai", "base_url": "https://model.example.test/v1", "model": "fixture", "key_configured": False}],
        "active_model_connection_id": "fixture",
        "options": {"industry_groups": [], "mail_providers": []},
        "module_readiness": {"assistant": {"status": "missing_model"},
            "discovery": {"ready": False, "missing": ["title_keywords", "industry_groups"]}},
        "onboarding": {"ready": False, "messages": {"resume": "岗位匹配资料待填写"}},
    }
    original_profile = json.loads(json.dumps(config["profile"]))
    health = {"enabled": False, "ready": False, "state": "failed", "detail": "Codex App Server missing key"}

    def route_request(route):
        request = route.request
        url = urlsplit(request.url)
        assert url.netloc == "ui.example.test"
        calls.append((url.path, request.post_data))
        filename = url.path.lstrip("/") or "index.html"
        if filename in {"index.html", "app.js", "configuration.js", "company-sources.js", "styles.css"}:
            mime = "text/html" if filename.endswith("html") else "text/css" if filename.endswith("css") else "application/javascript"
            return route.fulfill(body=(WEB / filename).read_text(encoding="utf-8"), content_type=mime)
        if url.path == "/api/local-ui/configuration/read":
            return route.fulfill(json=config)
        if url.path == "/api/codex/health":
            return route.fulfill(json=health)
        if url.path == "/api/local-ui/configuration/save":
            body = json.loads(request.post_data)
            assert "profile" not in body and "complete_onboarding" not in body
            assert set(body["settings"]) <= {"llm_enabled", "codex_runtime_enabled"}
            config["model_connections"][0]["key_configured"] = True
            config["module_readiness"]["assistant"]["status"] = "disabled" if body["settings"].get("codex_runtime_enabled") is False else "restart_required"
            config["restart_required"] = True
            return route.fulfill(json={"message": "模型已保存，重启桌面后生效", "restart_required": True})
        return route.fulfill(status=503, json={"detail": "Unavailable fixture"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.environ.get("RECRUITOPS_TEST_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": width, "height": 900}, service_workers="block")
        context.route("**/*", route_request)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://ui.example.test/", wait_until="networkidle")
        expect(page.locator('[data-view="knowledge"]')).to_have_count(0)
        expect(page.locator('[id^="assistant-knowledge"]')).to_have_count(0)
        assert not any("knowledge" in path for path, _ in calls)
        page.route("**/api/jobs/job-fixture", lambda route: route.fulfill(json={"title": "Fixture job"}))
        page.evaluate("""() => {
            document.getElementById('assistant-job-id').value = 'job-fixture';
            window.dispatchEvent(new CustomEvent('recruitops:assistant-job', {detail: {id: 'job-fixture'}}));
        }""")
        expect(page.locator("#assistant-selected-job")).to_have_text("Fixture job")
        page.evaluate("document.getElementById('assistant-clear-job').click()")
        expect(page.locator("#assistant-job-id")).to_have_value("")
        expect(page.locator("#assistant-selected-job")).to_be_hidden()
        if width < 700:
            page.locator("#mobile-menu-button").click()
        page.locator('[data-view="assistant"]').click()
        expect(page.locator("#assistant-availability")).to_have_attribute("data-state", "missing_model")
        expect(page.locator("#assistant-availability-detail")).to_contain_text("尚未配置模型连接")
        expect(page.locator("#assistant-messages")).not_to_contain_text("Codex")
        expect(page.locator("#run-task-button")).to_be_disabled()
        page.screenshot(path=str(tmp_path / f"assistant-missing-model-{width}.png"))
        page.locator("#assistant-open-configuration").click()
        expect(page.locator("#configuration-view")).to_be_visible()
        page.locator('[data-model-field="api_key"]').fill("synthetic-key")
        page.locator("#model-connection-save").click()
        expect(page.locator("#model-connection-message")).to_contain_text("模型已保存")
        saved = json.loads(next(body for path, body in calls if path.endswith("/configuration/save")))
        assert saved["settings"] == {}  # Backend owns automatic defaults, not false checkboxes.
        assert config["profile"] == original_profile
        if width < 700:
            page.locator("#mobile-menu-button").click()
        page.locator('[data-view="assistant"]').click()
        expect(page.locator("#assistant-availability")).to_have_attribute("data-state", "restart_required")
        expect(page.locator("#assistant-availability-detail")).to_contain_text("不会自动重启")
        config["module_readiness"]["assistant"]["status"] = "configured"
        page.locator("#assistant-recheck").click()
        expect(page.locator("#assistant-availability")).to_have_attribute("data-state", "runtime_failed")
        expect(page.locator("#assistant-availability-detail")).to_contain_text("本地助理服务连接失败")
        page.screenshot(path=str(tmp_path / f"assistant-service-failed-{width}.png"))
        health.update(enabled=True, ready=True, state="ready")
        page.locator("#assistant-recheck").click()
        expect(page.locator("#run-task-button")).to_be_enabled()
        expect(page.locator("#assistant-availability")).not_to_be_visible()
        assert config["profile"]["matching"]["title_keywords"] == []
        if width < 700:
            page.locator("#mobile-menu-button").click()
        page.locator('[data-view="configuration"]').click()
        for value in ["", "  \n\t"]:
            saves_before = sum(path.endswith("/configuration/save") for path, _ in calls)
            page.locator('[name="title_keywords"]').fill(value)
            page.locator("#configuration-save").click()
            expect(page.locator("#title-keywords-error")).to_be_visible()
            expect(page.locator("#title-keywords-error")).to_contain_text("至少填写一个岗位标题关键词")
            page.screenshot(path=str(tmp_path / f"keywords-required-{width}.png"))
            expect(page.locator('[name="title_keywords"]')).to_be_focused()
            assert sum(path.endswith("/configuration/save") for path, _ in calls) == saves_before
        page.locator("#assistant-advanced-options summary").click()
        page.locator("#configuration-assistant-mode").select_option("disabled")
        page.locator("#model-connection-save").click()
        expect(page.locator("#configuration-model-status")).to_have_text("已主动关闭")
        disabled = json.loads([body for path, body in calls if path.endswith("/configuration/save")][-1])
        assert disabled["settings"] == {"llm_enabled": False, "codex_runtime_enabled": False}
        assert not any("/turns" in path or path.endswith(("/model/test", "/mail/test")) for path, _ in calls)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors
        browser.close()


@pytest.mark.parametrize("width", [1440, 390])
def test_manual_form_and_model_connections_offline(width, tmp_path):
    calls, errors = [], []
    config = {
        "settings": {"llm_enabled": False, "codex_runtime_enabled": False,
            "job_analysis_enabled": False, "mail_enabled": False,
            "automation_enabled": False, "vision_enabled": False, "mail_sync_on_startup": False,
            "mail_imap_host": "", "mail_imap_port": 993, "mail_imap_username": "",
            "mail_imap_mailbox": "INBOX"},
        "secrets": {"mail_imap_password": False},
        "onboarding": {"ready": False, "missing": ["model", "resume", "title_keywords"],
            "messages": {"model": "请先配置模型连接", "resume": "请先上传简历", "title_keywords": "请确认关键词"}},
        "profile": {"degree": None, "skills": [], "matching": {"title_keywords": ["Python"]},
            "scope": {"industry_groups": []}},
        "model_connections": [{"id": "test", "name": "Test connection",
            "provider": "openai-compatible", "api_style": "openai",
            "base_url": "https://model.example.test", "model": "test-model", "key_configured": False}],
        "active_model_connection_id": "test",
        "options": {"industry_groups": [{"code": "test", "label": "Test industry"}],
            "mail_providers": [{"id": "custom", "label": "Custom", "host": "", "port": 993}]},
        "module_readiness": {"assistant": {"status": "missing_model"},
            "job_scoring": {"status": "not_ready", "ready": False, "message": "请先配置模型"},
            "scheduled_tasks": {"status": "not_ready", "ready": False, "message": "请先配置模型"},
            "mail": {"status": "not_ready", "ready": False, "message": "请填写 IMAP"}},
    }
    manual_attempts = 0
    applications = []
    events = []

    def route_request(route):
        nonlocal manual_attempts
        request = route.request
        url = urlsplit(request.url)
        assert url.netloc == "ui.example.test", "No real network allowed"
        calls.append((request.method, url.path, request.post_data))
        filename = url.path.lstrip("/") or "index.html"
        if filename in {"index.html", "app.js", "configuration.js", "company-sources.js", "styles.css"}:
            mime = "text/html" if filename.endswith("html") else "text/css" if filename.endswith("css") else "application/javascript"
            return route.fulfill(body=(WEB / filename).read_text(encoding="utf-8"), content_type=mime)
        if url.path == "/api/local-ui/configuration/read":
            return route.fulfill(json=config)
        if url.path == "/api/local-ui/configuration/save":
            return route.fulfill(json={"message": "Saved fixture"})
        if url.path == "/api/local-ui/applications/manual":
            assert request.headers["x-recruitops-local-ui"] == "1"
            manual_attempts += 1
            if manual_attempts == 1:
                return route.fulfill(status=403, json={"detail": "Writes are disabled"})
            applications.append({**json.loads(request.post_data), "id": "fixture",
                "updated_at": "2026-09-18T00:00:00Z", "stage_history": []})
            return route.fulfill(json={"created": True, "application_id": "fixture", "stage": "applied"})
        if url.path == "/api/applications/page":
            return route.fulfill(json={"items": applications, "total": len(applications)})
        if url.path == "/api/jobs/browse":
            relevant = {"id": "related", "company_id": "example", "company_name": "Example",
                "title": "Relevant unscored engineer", "match_score": None, "analysis_status": "eligible",
                "category_label": "Engineering", "platform": "fixture", "batch": "formal",
                "detail_url": "https://example.test/job"}
            unrelated = {**relevant, "id": "excluded", "title": "Unrelated excluded position",
                "analysis_status": "direction_out", "match_score": 90}
            return route.fulfill(json={"items": [relevant, unrelated], "featured": [unrelated], "total": 1,
                "stats": {"jobs": 1, "pending": 1},
                "facets": {"companies": [], "categories": {}, "platforms": []}})
        if url.path == "/api/local-ui/applications/fixture" and request.method == "PATCH":
            applications[0].update(json.loads(request.post_data))
            return route.fulfill(json={"status": "updated"})
        if url.path == "/api/schedule":
            return route.fulfill(json=events)
        if url.path == "/api/local-ui/events" and request.method == "POST":
            events.append({**json.loads(request.post_data), "id": "event-fixture", "status": "pending",
                "updated_at": "2026-09-18T00:00:00Z"})
            return route.fulfill(json={"status": "created"})
        if url.path == "/api/local-ui/events/event-fixture" and request.method == "PATCH":
            events[0].update(json.loads(request.post_data))
            return route.fulfill(json={"status": "updated"})
        return route.fulfill(status=503, json={"detail": "Offline fixture: unavailable"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.environ.get("RECRUITOPS_TEST_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": width, "height": 900}, service_workers="block")
        context.add_init_script("""window.recruitopsDesktop = Object.freeze({
            onApplicationDraft(callback) {
                window.__fixtureDraft = callback;
                callback({company_name:'',job_title:'Queued shell draft'});
            }
        });""")
        context.route("**/*", route_request)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://ui.example.test/", wait_until="networkidle")
        expect(page.locator("#application-create-dialog")).to_be_visible()
        expect(page.locator("#applications-view")).to_be_visible()
        expect(page.locator('#application-create-form [name="job_title"]')).to_have_value("Queued shell draft")
        assert not any(path.endswith("/applications/manual") for _, path, _ in calls)
        page.locator("#application-create-cancel").click()
        page.evaluate("window.postMessage({type:'desktop:application-draft',job_title:'Untrusted'}, '*')")
        expect(page.locator("#application-create-dialog")).not_to_be_visible()
        page.evaluate("window.__fixtureDraft({company_name:'',job_title:'Captured engineer',note:'https://example.test/job'})")
        expect(page.locator("#application-create-dialog")).to_be_visible()
        expect(page.locator('#application-create-form [name="company_name"]')).to_have_value("")
        expect(page.locator('#application-create-form [name="record_url"]')).to_have_value("")
        page.locator('#application-create-form [name="job_title"]').fill("Unsaved user edit")
        page.evaluate("window.__fixtureDraft({company_name:'Other',job_title:'Another draft'})")
        expect(page.locator('#application-create-form [name="job_title"]')).to_have_value("Unsaved user edit")
        assert not any(path.endswith("/applications/manual") for _, path, _ in calls)
        page.locator("#application-create-cancel").click()
        page.evaluate("window.__fixtureDraft({company_name:'Other',job_title:'Invalid',token:'forbidden'})")
        expect(page.locator("#application-create-dialog")).not_to_be_visible()
        if width < 700:
            page.locator("#mobile-menu-button").click()
        page.locator('[data-view="applications"]').click()
        page.locator("#application-add-button").click()
        form = page.locator("#application-create-form")
        form.locator('[name="company_name"]').fill(" Example ")
        form.locator('[name="job_title"]').fill(" Engineer ")
        page.locator("#application-create-submit").click()
        expect(page.locator("#application-create-error")).to_contain_text("Writes are disabled")
        expect(page.locator("#application-create-dialog")).to_be_visible()
        expect(page.locator("#application-create-submit")).to_be_enabled()
        page.screenshot(path=str(tmp_path / f"manual-{width}.png"))
        page.locator("#application-create-submit").click()
        expect(page.locator("#application-create-dialog")).not_to_be_visible()
        posted = [json.loads(body) for method, path, body in calls if path.endswith("/applications/manual")]
        assert posted[-1] == {"company_name": "Example", "job_title": "Engineer", "stage": "applied", "record_url": None, "note": ""}
        assert not any("/turns" in path for _, path, _ in calls)
        assert any(path == "/api/applications/page" for _, path, _ in calls)
        expect(page.locator(".application-card")).to_have_count(1)
        page.get_by_role("button", name="编辑 Example 的投递记录", exact=True).click()
        editor = page.locator(".application-card").locator("form").first
        editor.locator("select").first.select_option("written")
        editor.get_by_role("button", name="更新", exact=True).click()
        expect(page.get_by_role("button", name="编辑 Example 的投递记录", exact=True)).to_have_attribute("aria-expanded", "false")
        expect(page.locator(".application-card").locator("form").first.locator("select").first).to_have_value("written")
        assert applications[0]["stage"] == "written"
        page.screenshot(path=str(tmp_path / f"applications-{width}.png"))
        if width < 700:
            page.locator("#mobile-menu-button").click()
        page.locator('[data-view="schedule"]').click()
        page.locator("#schedule-add-button").click()
        page.locator("#schedule-title").fill("Fixture interview")
        page.locator("#schedule-event-type").fill("interview")
        page.locator("#schedule-company-name").fill("Example")
        page.locator("#schedule-editor-submit").click()
        expect(page.locator("#schedule-editor-dialog")).not_to_be_visible()
        expect(page.locator("#schedule-todo-view")).to_contain_text("Fixture interview")
        page.screenshot(path=str(tmp_path / f"schedule-pending-{width}.png"))
        page.locator('[data-schedule-action="complete"]').first.click()
        expect(page.locator("#nav-schedule-count")).to_have_text("0")
        assert events[0]["status"] == "completed"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"schedule-{width}.png"))
        if width < 700:
            page.locator("#mobile-menu-button").click()
        page.locator('[data-view="configuration"]').click()
        expect(page.locator("#configuration-model-status")).to_have_text("待配置模型")
        expect(page.locator("#configuration-analysis-status")).to_have_text("未就绪")
        expect(page.locator("#configuration-mail-status")).to_have_text("可选，未配置")
        expect(page.locator("#configuration-readiness")).to_have_text("首次配置尚未完成")
        expect(page.locator("#configuration-missing")).to_contain_text("请先上传简历")
        expect(page.locator('[name="degree"]')).to_have_value("")
        expect(page.locator("#configuration-assistant-mode")).to_have_value("auto")
        for field in ("mail_enabled", "job_analysis_enabled", "automation_enabled"):
            expect(page.locator(f'[name="{field}"]')).to_have_count(0)
        key = page.locator('[data-model-field="api_key"]').first
        key.fill("synthetic-key-for-offline-test")
        page.locator("#model-connection-add").click()
        expect(page.locator(".model-connection")).to_have_count(2)
        expect(page.locator('[data-model-field="api_key"]').first).to_have_value("synthetic-key-for-offline-test")
        page.locator(".model-connection").last.get_by_role("button", name="删除", exact=True).click()
        expect(page.locator('[data-model-field="api_key"]').first).to_have_value("synthetic-key-for-offline-test")
        page.locator('#configuration-industry-groups input').check()
        page.locator("#configuration-runtime-options summary").click()
        expect(page.locator('[name="vision_enabled"]')).not_to_be_checked()
        expect(page.locator('[name="mail_sync_on_startup"]')).to_have_count(0)
        page.locator('[name="title_keywords"]').fill("Python")
        page.locator("#configuration-save").click()
        expect(page.locator("#configuration-save-result")).to_have_text("Saved fixture")
        saved = json.loads(next(body for _, path, body in calls if path.endswith("/configuration/save")))
        assert saved["active_model_connection_id"] == "test"
        assert saved["profile"]["scope"]["industry_groups"] == ["test"]
        assert "write_enabled" not in saved["settings"]
        assert "llm_enabled" not in saved["settings"]
        assert "codex_runtime_enabled" not in saved["settings"]
        for field in ("mail_enabled", "job_analysis_enabled", "automation_enabled"):
            assert field not in saved["settings"]
        assert saved["settings"]["vision_enabled"] is False
        assert "mail_sync_on_startup" not in saved["settings"]
        assert saved["profile"]["degree"] is None
        assert "complete_onboarding" not in saved
        assert saved["model_connections"][0]["api_key"] == "synthetic-key-for-offline-test"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"configuration-{width}.png"), full_page=True)
        page.locator('[name="vision_enabled"]').check()
        page.locator('#configuration-industry-groups input').check()
        saves_before_model = sum(path.endswith("/configuration/save") for _, path, _ in calls)
        page.locator("#configuration-save").click()
        expect(page.locator("#configuration-save-result")).to_contain_text("请完成主模型连接配置")
        assert sum(path.endswith("/configuration/save") for _, path, _ in calls) == saves_before_model
        config["configured_capabilities"] = {"llm_enabled": True, "codex_runtime_enabled": True,
            "job_analysis_enabled": True, "mail_enabled": False, "automation_enabled": True,
            "vision_enabled": True, "mail_sync_on_startup": False}
        config["model_connections"][0]["key_configured"] = True
        config["module_readiness"]["assistant"]["status"] = "restart_required"
        config["module_readiness"]["job_scoring"].update(status="configured", ready=True)
        config["module_readiness"]["scheduled_tasks"].update(status="configured", ready=True)
        config["restart_required"] = True
        page.locator("#configuration-reload").click()
        expect(page.locator("#configuration-assistant-mode")).to_have_value("auto")
        expect(page.locator("#configuration-model-status")).to_have_text("待重启")
        expect(page.locator('[name="vision_enabled"]')).to_be_checked()
        expect(page.locator("#configuration-runtime-status")).to_have_text("定时任务：已开启 · 浏览器截图识别：未启用 · 邮箱：未配置（可选）")
        expect(page.locator("#configuration-message")).to_have_text("")
        page.locator('#configuration-industry-groups input').check()
        saves_before = sum(path.endswith("/configuration/save") for _, path, _ in calls)
        page.locator("#configuration-complete").click()
        expect(page.locator("#configuration-save-result")).to_have_text("Saved fixture")
        assert sum(path.endswith("/configuration/save") for _, path, _ in calls) == saves_before + 1
        expect(page.locator('[name="mail_sync_on_startup"]')).to_have_count(0)
        page.locator('[name="mail_imap_host"]').fill("imap.example.test")
        page.locator('[name="mail_imap_username"]').fill("fixture@example.test")
        page.locator('[name="mail_imap_password"]').fill("synthetic-mail-secret")
        page.locator('#configuration-industry-groups input').check()
        page.locator("#configuration-complete").click()
        expect(page.locator("#configuration-save-result")).to_have_text("Saved fixture")
        completion = json.loads([body for _, path, body in calls if path.endswith("/configuration/save")][-1])
        assert completion["complete_onboarding"] is True
        assert "llm_enabled" not in completion["settings"]
        assert "codex_runtime_enabled" not in completion["settings"]
        for field in ("mail_enabled", "job_analysis_enabled", "automation_enabled"):
            assert field not in completion["settings"]
        assert completion["settings"]["vision_enabled"] is True
        assert "mail_sync_on_startup" not in completion["settings"]
        assert "write_enabled" not in completion["settings"]
        # Saved secrets and configured flags survive readback without being live yet.
        config["settings"].update(mail_imap_host="imap.example.test", mail_imap_username="fixture@example.test")
        config["secrets"]["mail_imap_password"] = True
        config["configured_capabilities"].update(mail_enabled=True, mail_sync_on_startup=True)
        config["module_readiness"]["mail"].update(status="configured", ready=True)
        page.locator("#configuration-reload").click()
        expect(page.locator('[name="mail_sync_on_startup"]')).to_have_count(0)
        expect(page.locator('[name="mail_imap_password"]')).to_have_value("")
        expect(page.locator("#configuration-mail-status")).to_have_text("已配置")
        page.locator("#assistant-advanced-options summary").click()
        page.locator("#configuration-assistant-mode").select_option("disabled")
        expect(page.locator('[name="vision_enabled"]')).not_to_be_checked()
        page.locator('#configuration-industry-groups input').check()
        page.locator("#configuration-save").click()
        expect(page.locator("#configuration-save-result")).to_have_text("Saved fixture")
        disabled = json.loads([body for _, path, body in calls if path.endswith("/configuration/save")][-1])
        assert disabled["settings"]["llm_enabled"] is False
        assert disabled["settings"]["codex_runtime_enabled"] is False
        assert disabled["settings"]["vision_enabled"] is False
        assert "mail_sync_on_startup" not in disabled["settings"]
        for field in ("job_analysis_enabled", "mail_enabled", "automation_enabled"):
            assert field not in disabled["settings"]
        assert "complete_onboarding" not in disabled
        assert not any(path.endswith(("/model/test", "/mail/test")) for _, path, _ in calls)
        expect(page.locator("#configuration-restart-policy")).to_contain_text("保存不会调用模型、同步邮箱或自动重启")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"runtime-options-{width}.png"), full_page=True)
        config["onboarding"] = {"ready": True, "missing": [], "messages": {}}
        page.locator("#configuration-reload").click()
        expect(page.locator("#configuration-readiness")).to_have_text("基础配置已就绪")
        expect(page.locator("#onboarding-notice")).not_to_be_visible()
        config.pop("model_connections")
        page.locator("#configuration-reload").click()
        expect(page.locator("#configuration-message")).to_contain_text("配置 API 尚未支持")
        expect(page.locator("#configuration-model-status")).to_have_text("状态不可用")
        if width < 700:
            page.locator("#mobile-menu-button").click()
        expect(page.locator('#sidebar [data-view="jobs"]')).to_have_count(1)
        expect(page.locator('[data-job-nav-mode="today"]')).to_have_count(0)
        expect(page.locator("#nav-today-count")).to_have_count(0)
        page.locator('#sidebar [data-view="jobs"]').click()
        expect(page.locator('#job-evaluation-filter option[value="excluded"]')).to_have_count(0)
        with page.expect_response(lambda response: "/api/jobs/browse?" in response.url and "first_seen_on=" in response.url):
            page.locator('[data-job-mode="today"]').click()
        expect(page.locator("#jobs-heading")).to_have_text("今日新增岗位")
        expect(page.locator("#jobs-table-body")).to_contain_text("Relevant unscored engineer")
        expect(page.locator("#jobs-table-body")).not_to_contain_text("Unrelated excluded position")
        expect(page.locator("#featured-job-list")).not_to_contain_text("Unrelated excluded position")
        with page.expect_response(lambda response: "/api/jobs/browse?" in response.url and "evaluation=unscored" in response.url):
            page.locator("#job-evaluation-filter").select_option("unscored")
        expect(page.locator("#jobs-table-body")).to_contain_text("Relevant unscored engineer")
        with page.expect_response(lambda response: "/api/jobs/browse?" in response.url and "first_seen_on=" not in response.url):
            page.locator('[data-job-mode="all"]').click()
        expect(page.locator("#jobs-heading")).to_have_text("27届校招岗位")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"related-jobs-{width}.png"))
        assert not errors
        browser.close()
