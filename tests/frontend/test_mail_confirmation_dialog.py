"""Offline user confirmation flow. Every network response and write is mocked."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright


WEB = Path(__file__).resolve().parents[2] / "apps" / "web"
THREAD = "thread-mail-test"
RUN = "mail-process-test"
DIGEST = "b" * 64


class MailDialogFixture:
    def __init__(self):
        self.items = [self.item("mail-one"), self.item("mail-two")]
        self.calls = []
        self.fail_execute = False
        self.fail_resolve = False
        self.fail_approval_read = False
        self.hold_confirmation = False
        self.pending_confirmation = None
        self.hold_proposal = False
        self.pending_proposal = None
        self.candidate_digest = DIGEST
        self.hold_query = None
        self.pending_candidates = None
        self.proposals = 0

    @staticmethod
    def item(record_id):
        return {"record_id": record_id, "subject": f"Invitation {record_id}", "content_digest": DIGEST}

    def queue(self):
        return {"runs": [{"run_id": RUN, "thread_id": THREAD,
                           "status": "awaiting_confirmation" if self.items else "running",
                           "items": self.items, "report_pending": False}]}

    def route(self, route):
        url = urlsplit(route.request.url)
        assert url.netloc == "ui.example.test", "No live network or model access is permitted"
        filename = url.path.lstrip("/") or "index.html"
        if filename in {"index.html", "app.js", "styles.css", "configuration.js", "company-sources.js"}:
            source = (WEB / filename).read_text(encoding="utf-8")
            if filename == "app.js":
                source = source.replace("const testHooks = {", "const testHooks = { refreshMailConfirmations,")
                source = source.replace("if (globalThis.__RECRUITOPS_TEST_MODE__) {",
                                        "globalThis.mailDialogHooks = testHooks; if (globalThis.__RECRUITOPS_TEST_MODE__) {")
            mime = "text/html" if filename.endswith("html") else "text/css" if filename.endswith("css") else "application/javascript"
            return route.fulfill(body=source, content_type=mime)
        body = route.request.post_data_json if route.request.method == "POST" else None
        self.calls.append((route.request.method, url.path, parse_qs(url.query), body))
        if url.path == "/api/local-ui/mail-confirmations":
            if self.hold_confirmation:
                self.pending_confirmation = route
                return
            query = parse_qs(url.query)
            return route.fulfill(json=self.queue() if query.get("thread_id") == [THREAD] else {"runs": []})
        if url.path.endswith("/binding-candidates"):
            query = parse_qs(url.query)
            offset = int(query.get("offset", [0])[0])
            record_id = url.path.split("/")[3]
            search = query.get("query", [""])[0]
            candidates = [
                {"application_id": f"application-{index}", "company_name": f"Company {index}",
                 "job_title": f"Engineer {index}", "city": "深圳", "stage": "applied",
                 "reason": "company_match"}
                for index in range(21)
            ]
            if search:
                candidates = [candidate for candidate in candidates if search.casefold() in f"{candidate['company_name']} {candidate['job_title']}".casefold()]
            payload = {"record_id": record_id, "subject": f"Invitation {record_id}",
                                       "excerpt": "Please attend the written test. <script>untrusted()</script>",
                                       "received_at": "2026-09-29T10:00:00Z", "content_digest": self.candidate_digest,
                                       "binding_revision": 0, "current_application_id": None,
                                       "candidates": candidates[offset:offset + 20], "total": len(candidates),
                                       "has_more": offset + 20 < len(candidates)}
            if search and search == self.hold_query:
                self.pending_candidates = (route, payload)
                return
            return route.fulfill(json=payload)
        if url.path.endswith("/binding-proposals"):
            self.proposals += 1
            payload = {"success": True, "data": {"approval_id": f"proposal-{self.proposals}",
                                                 "approval_status": "pending", "preview": {"after": body}}}
            if self.hold_proposal:
                self.pending_proposal = (route, payload)
                return
            return route.fulfill(json=payload)
        if url.path.endswith("/approve"):
            return route.fulfill(json={"allowed": True, "status": "approved"})
        if url.path.endswith("/execute"):
            return route.fulfill(json={"success": not self.fail_execute})
        if url.path == f"/api/local-ui/mail-confirmations/{RUN}/resolve":
            if self.fail_resolve:
                return route.fulfill(status=503, json={"detail": "Synthetic resume unavailable"})
            assert body["thread_id"] == THREAD
            assert body["content_digest"] == DIGEST
            self.items = [item for item in self.items if item["record_id"] != body["record_id"]]
            return route.fulfill(json={"status": "accepted", "run_id": RUN})
        if url.path == "/api/approvals":
            return route.fulfill(status=503, json={"detail": "Synthetic approval list read failure"}) if self.fail_approval_read else route.fulfill(json=[])
        if url.path == "/api/recruitment-mails":
            return route.fulfill(json={"items": [], "total": 0})
        if url.path == "/health":
            return route.fulfill(json={"status": "ok", "mode": "read_only"})
        if url.path == "/api/codex/health":
            return route.fulfill(json={"enabled": False, "ready": False})
        if url.path in {"/api/schedule", "/api/companies", "/api/codex/traces"}:
            return route.fulfill(json=[])
        return route.fulfill(status=503, json={"detail": "Offline fixture: unavailable"})

    def mutations(self):
        return [call for call in self.calls if call[0] == "POST" and (call[1].endswith(("/binding-proposals", "/approve", "/execute", "/resolve")))]


@pytest.fixture
def mail_dialog():
    fixture = MailDialogFixture()
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        context.route("**/*", fixture.route)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://ui.example.test/", wait_until="networkidle")
        assert not errors, errors
        page.evaluate("mailDialogHooks.state.codexThreadId = 'thread-mail-test'")
        page.locator('[data-view="assistant"]').click()
        page.evaluate("mailDialogHooks.refreshMailConfirmations()")
        yield fixture, page, page.locator("#mail-binding-dialog")
        assert not errors
        browser.close()


def open_pending(page, dialog):
    page.locator("#assistant-mail-binding-approvals").get_by_role("button").click()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("radio")).to_have_count(20)


def test_candidates_require_selection_and_search_is_paged_read_only(mail_dialog):
    fixture, page, dialog = mail_dialog
    open_pending(page, dialog)
    expect(page.locator("dialog[open]")).to_have_count(1)
    expect(dialog.locator("input:checked")).to_have_count(0)
    expect(dialog.get_by_role("button", name="确认关联并继续处理", exact=True)).to_be_disabled()
    expect(dialog).to_contain_text("Company 0 · Engineer 0")
    expect(dialog).to_contain_text("深圳 · 已投递")
    dialog.get_by_text("查看邮件内容摘要", exact=True).click()
    expect(dialog.locator("pre")).to_contain_text("<script>untrusted()</script>")
    expect(dialog.locator("script")).to_have_count(0)
    dialog.get_by_role("radio").first.check()
    expect(dialog.get_by_role("button", name="确认关联并继续处理", exact=True)).to_be_enabled()
    dialog.get_by_role("button", name="下一页", exact=True).click()
    expect(dialog.get_by_role("radio")).to_have_count(1)
    expect(dialog).to_contain_text("Company 20 · Engineer 20")
    expect(dialog).to_contain_text("已选择 1 条投递")
    expect(dialog.get_by_role("button", name="确认关联并继续处理", exact=True)).to_be_enabled()
    dialog.get_by_role("searchbox").fill("Engineer 3")
    dialog.get_by_role("button", name="搜索", exact=True).click()
    expect(dialog.get_by_role("radio")).to_have_count(1)
    expect(dialog).to_contain_text("Company 3 · Engineer 3")
    expect(dialog.locator("input:checked")).to_have_count(0)
    expect(dialog.locator(".mail-binding-selected")).to_contain_text("Company 0 · Engineer 0")
    assert not fixture.mutations()
    assert any(call[2].get("offset") == ["20"] for call in fixture.calls)
    assert any(call[2].get("query") == ["Engineer 3"] and call[2].get("offset") == ["0"] for call in fixture.calls)


def test_one_dialog_confirms_in_order_and_advances_without_duplicate_execution(mail_dialog):
    fixture, page, dialog = mail_dialog
    open_pending(page, dialog)
    dialog.get_by_role("radio").nth(2).check()
    # Two synchronous click events cannot issue two proposal/execute requests.
    dialog.get_by_role("button", name="确认关联并继续处理", exact=True).evaluate("button => { button.click(); button.click(); }")
    expect(dialog).to_contain_text("Invitation mail-two")
    expect(page.locator("dialog[open]")).to_have_count(1)
    calls = fixture.mutations()
    assert [call[1].rsplit("/", 1)[1] for call in calls] == ["binding-proposals", "approve", "execute", "resolve"]
    assert calls[0][3]["application_id"] == "application-2"
    assert calls[-1][3]["record_id"] == "mail-one"
    assert calls[-1][3]["action"] == "confirmed"
    expect(dialog.locator("input:checked")).to_have_count(0)
    dialog.get_by_role("button", name="本封不关联", exact=True).click()
    expect(dialog).not_to_be_visible()
    assert fixture.mutations()[-1][3]["action"] == "rejected"
    assert sum(call[1].endswith("/execute") for call in fixture.mutations()) == 1
    assert fixture.items == []


def test_execute_failure_keeps_selection_and_does_not_resolve_until_retry(mail_dialog):
    fixture, page, dialog = mail_dialog
    fixture.fail_execute = True
    open_pending(page, dialog)
    dialog.get_by_role("radio").first.check()
    dialog.get_by_role("button", name="确认关联并继续处理", exact=True).click()
    expect(page.locator("#toast-region")).to_contain_text("关联未保存")
    expect(dialog).to_be_visible()
    expect(dialog.locator("input:checked")).to_have_count(1)
    assert not any(call[1].endswith("/resolve") for call in fixture.mutations())
    fixture.fail_execute = False
    dialog.get_by_role("button", name="确认关联并继续处理", exact=True).click()
    expect(dialog).to_contain_text("Invitation mail-two")
    assert sum(call[1].endswith("/resolve") for call in fixture.mutations()) == 1


def test_resume_failure_retries_without_rebinding(mail_dialog):
    fixture, page, dialog = mail_dialog
    fixture.fail_resolve = True
    open_pending(page, dialog)
    dialog.get_by_role("radio").first.check()
    dialog.get_by_role("button", name="确认关联并继续处理", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("关联已保存")
    expect(dialog.get_by_role("alert")).to_contain_text("Synthetic resume unavailable")
    fixture.fail_resolve = False
    dialog.get_by_role("button", name="重试续办", exact=True).click()
    expect(dialog).to_contain_text("Invitation mail-two")
    assert fixture.proposals == 1
    assert sum(call[1].endswith("/execute") for call in fixture.mutations()) == 1
    assert sum(call[1].endswith("/resolve") for call in fixture.mutations()) == 2


def test_closing_pending_dialog_is_not_rejection_and_does_not_reopen_on_poll(mail_dialog):
    fixture, page, dialog = mail_dialog
    page.evaluate("mailDialogHooks.refreshMailConfirmations({autoOpen: true})")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="关闭，稍后处理邮件关联", exact=True).click()
    expect(dialog).not_to_be_visible()
    page.evaluate("mailDialogHooks.refreshMailConfirmations({autoOpen: true})")
    expect(dialog).not_to_be_visible()
    expect(page.locator("#assistant-mail-binding-approvals")).to_contain_text("待确认 2")
    assert not fixture.mutations()
    open_pending(page, dialog)
    dialog.press("Escape")
    expect(dialog).not_to_be_visible()
    assert not fixture.mutations()


def test_manual_close_stays_dismissed_across_refresh_and_restart(mail_dialog):
    fixture, page, dialog = mail_dialog
    open_pending(page, dialog)
    dialog.get_by_role("button", name="关闭，稍后处理邮件关联", exact=True).click()
    page.evaluate("mailDialogHooks.refreshMailConfirmations({autoOpen: true})")
    expect(dialog).not_to_be_visible()
    page.reload(wait_until="networkidle")
    page.evaluate("mailDialogHooks.state.codexThreadId = 'thread-mail-test'")
    page.evaluate("mailDialogHooks.refreshMailConfirmations({autoOpen: true})")
    expect(dialog).not_to_be_visible()
    assert not fixture.mutations()


def test_pagination_and_close_cannot_change_an_inflight_confirmation(mail_dialog):
    fixture, page, dialog = mail_dialog
    fixture.hold_proposal = True
    open_pending(page, dialog)
    dialog.get_by_role("radio").first.check()
    dialog.get_by_role("button", name="确认关联并继续处理", exact=True).click()
    page.wait_for_function("true")
    assert fixture.pending_proposal is not None
    try:
        candidate_reads = sum(call[1].endswith("/binding-candidates") for call in fixture.calls)
        dialog.get_by_role("button", name="下一页", exact=True).evaluate("button => button.click()")
        dialog.press("Escape")
        expect(dialog).to_be_visible()
        assert sum(call[1].endswith("/binding-candidates") for call in fixture.calls) == candidate_reads
    finally:
        pending, payload = fixture.pending_proposal
        pending.fulfill(json=payload)
    expect(dialog).to_contain_text("Invitation mail-two")
    assert sum(call[1].endswith("/resolve") for call in fixture.mutations()) == 1


def test_successful_resolve_is_not_relabelled_failed_when_approval_list_refresh_fails(mail_dialog):
    fixture, page, dialog = mail_dialog
    open_pending(page, dialog)
    fixture.fail_approval_read = True
    dialog.get_by_role("radio").first.check()
    dialog.get_by_role("button", name="确认关联并继续处理", exact=True).click()
    expect(dialog).to_contain_text("Invitation mail-two", timeout=3000)
    expect(dialog.get_by_role("alert")).not_to_contain_text("续办尚未启动")
    assert sum(call[1].endswith("/resolve") for call in fixture.mutations()) == 1


def test_new_search_invalidates_older_candidate_response(mail_dialog):
    fixture, page, dialog = mail_dialog
    open_pending(page, dialog)
    fixture.hold_query = "Engineer 3"
    dialog.get_by_role("searchbox").fill("Engineer 3")
    dialog.get_by_role("button", name="搜索", exact=True).click()
    page.wait_for_function("true")
    assert fixture.pending_candidates is not None
    dialog.get_by_role("searchbox").fill("Engineer 5")
    dialog.get_by_role("button", name="搜索", exact=True).click()
    expect(dialog.get_by_role("radio")).to_have_count(1)
    expect(dialog).to_contain_text("Company 5 · Engineer 5")
    pending, payload = fixture.pending_candidates
    pending.fulfill(json=payload)
    expect(dialog).not_to_contain_text("Company 3 · Engineer 3")
    expect(dialog.locator("input:checked")).to_have_count(0)
    assert not fixture.mutations()


@pytest.mark.parametrize("changed", ["digest", "flag"])
def test_changed_mail_source_cannot_be_confirmed_using_previous_task_evidence(mail_dialog, changed):
    fixture, page, dialog = mail_dialog
    if changed == "digest":
        fixture.candidate_digest = "a" * 64
    else:
        fixture.items[0]["source_changed"] = True
        page.evaluate("mailDialogHooks.refreshMailConfirmations()")
    page.locator("#assistant-mail-binding-approvals").get_by_role("button").click()
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("邮件内容已变化")
    expect(dialog.get_by_role("radio")).to_have_count(0)
    expect(dialog.get_by_role("button", name="确认关联并继续处理", exact=True)).to_be_disabled()
    assert not fixture.mutations()


def test_other_thread_cannot_auto_open_or_apply_stale_confirmation_response(mail_dialog):
    fixture, page, dialog = mail_dialog
    page.evaluate("mailDialogHooks.state.codexThreadId = 'another-thread'")
    page.evaluate("mailDialogHooks.refreshMailConfirmations({autoOpen: true})")
    expect(dialog).not_to_be_visible()
    expect(page.locator("#assistant-mail-binding-approvals")).to_be_empty()
    page.evaluate("mailDialogHooks.state.codexThreadId = 'thread-mail-test'")
    fixture.hold_confirmation = True
    page.evaluate("void mailDialogHooks.refreshMailConfirmations({autoOpen: true})")
    page.wait_for_function("true")
    assert fixture.pending_confirmation is not None
    page.evaluate("mailDialogHooks.state.codexThreadId = 'another-thread'")
    fixture.pending_confirmation.fulfill(json=fixture.queue())
    page.evaluate("new Promise(resolve => setTimeout(resolve, 0))")
    expect(dialog).not_to_be_visible()
    expect(page.locator("#assistant-mail-binding-approvals")).to_be_empty()
    assert not fixture.mutations()
