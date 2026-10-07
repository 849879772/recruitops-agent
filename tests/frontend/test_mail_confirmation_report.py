"""Offline SSE confirmation reports: scoped, leased, read-only and retryable."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from test_mail_confirmation_dialog import MailDialogFixture, THREAD


REPORT_RUN = "c" * 32
REPORT_PREFIX = "[RecruitOps 邮件确认后只读汇报]"


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class MailReportFixture(MailDialogFixture):
    def __init__(self):
        super().__init__()
        self.items = []
        self.pending = True
        self.claimed = False
        self.allow_claim = True
        self.report_actions = []
        self.stream_calls = []
        self.fail_stream = False
        self.hold_stream = False
        self.empty_stream = False
        self.pending_stream = None
        self.hold_claim = False
        self.pending_claim = None
        self.run_thread = THREAD

    def queue(self):
        return {"runs": [{"run_id": REPORT_RUN, "thread_id": self.run_thread, "items": [],
                           "status": "succeeded", "confirmation_version": 3,
                           "report_pending": self.pending}]}

    @staticmethod
    def stream_body():
        base = {"thread_id": THREAD, "turn_id": "report-turn"}
        return "".join([
            sse("turn", {"id": "report-turn", "thread_id": THREAD}),
            sse("item_completed", {**base, "event_type": "item_completed", "event_id": "status-read",
                                   "payload": {"tool_name": "recruitment_mail_run_status", "output": {"run_id": REPORT_RUN, "status": "succeeded"}}}),
            sse("text_delta", {**base, "event_type": "text_delta", "event_id": "summary",
                               "text": f"已处理 2 封邮件，更新 1 条投递。{REPORT_RUN}"}),
            sse("turn_completed", {**base, "event_type": "turn_completed", "event_id": "finished"}),
        ])

    def route(self, route):
        url = urlsplit(route.request.url)
        if url.path == f"/api/local-ui/mail-confirmations/{REPORT_RUN}/report":
            body = route.request.post_data_json
            self.calls.append((route.request.method, url.path, parse_qs(url.query), body))
            self.report_actions.append(body)
            assert body["thread_id"] == THREAD
            assert body["version"] == 3
            if body["action"] == "claim":
                granted = self.allow_claim and self.pending and not self.claimed
                self.claimed = granted or self.claimed
                payload = {"claimed": granted, "claim_token": "claim-test-token" if granted else None}
                if self.hold_claim:
                    self.pending_claim = (route, payload)
                    return
                return route.fulfill(json=payload)
            assert body["claim_token"] == "claim-test-token"
            self.claimed = False
            if body["action"] == "complete":
                self.pending = False
            return route.fulfill(json={"success": True})
        if url.path.endswith("/turns/stream"):
            body = route.request.post_data_json
            self.stream_calls.append(body)
            self.calls.append((route.request.method, url.path, {}, body))
            assert url.path == f"/api/codex/threads/{THREAD}/turns/stream"
            if self.fail_stream:
                return route.fulfill(status=503, json={"detail": "Synthetic report model unavailable"})
            if self.empty_stream:
                return route.fulfill(body=sse("turn", {"id": "report-turn", "thread_id": THREAD})
                                     + sse("turn_completed", {"thread_id": THREAD, "turn_id": "report-turn",
                                                              "event_type": "turn_completed", "event_id": "finished-empty"}),
                                     content_type="text/event-stream")
            if self.hold_stream:
                self.pending_stream = route
                return
            return route.fulfill(body=self.stream_body(), content_type="text/event-stream")
        if url.path == "/api/codex/threads":
            return route.fulfill(json={"threads": []})
        if url.path == "/api/applications/page":
            return route.fulfill(json={"items": [], "total": 0, "unfiltered_total": 0, "stage_counts": {}})
        return super().route(route)


@pytest.fixture
def mail_report():
    fixture = MailReportFixture()
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        context.route("**/*", fixture.route)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("globalThis.__RECRUITOPS_TEST_MODE__ = true")
        page.goto("https://ui.example.test/")
        assert not errors
        page.evaluate("""() => {
          globalThis.reportHooks = __RECRUITOPS_TEST_HOOKS__;
          Object.assign(reportHooks.state, {codexThreadId: 'thread-mail-test', codexReady: true, codexEnabled: true});
          document.querySelector('#assistant-view').hidden = false;
        }""")
        yield fixture, page
        assert not errors
        browser.close()


def assert_no_mail_execution(fixture):
    # Reporting may start one model turn, but never a new mail run/resolve/write.
    posts = [call[1] for call in fixture.calls if call[0] == "POST"]
    readonly_configuration = {"/api/local-ui/configuration/read", "/api/local-ui/configuration/latest-crawl"}
    assert all(path.endswith(("/report", "/turns/stream")) or path in readonly_configuration for path in posts), posts
    for request in fixture.stream_calls:
        prompt = request["text"]
        assert prompt.startswith(REPORT_PREFIX)
        assert f'recruitment_mail_run_status(run_id="{REPORT_RUN}", wait_ms=0)' in prompt
        assert "不能重新启动或恢复处理" in prompt
        assert "不能写数据" in prompt
        assert "recruitment_mail_run_start(" not in prompt


def test_pending_report_is_claimed_once_streamed_read_only_and_completed(mail_report):
    fixture, page = mail_report
    fixture.hold_stream = True
    page.evaluate("reportHooks.refreshMailConfirmations()")
    page.wait_for_function("reportHooks.state.activeAssistantController !== null")
    page.evaluate("Promise.all([reportHooks.refreshMailConfirmations(), reportHooks.refreshMailConfirmations()])")
    assert [item["action"] for item in fixture.report_actions] == ["claim"]
    assert len(fixture.stream_calls) == 1
    assert fixture.pending_stream is not None
    fixture.pending_stream.fulfill(body=fixture.stream_body(), content_type="text/event-stream")
    expect(page.locator("#assistant-message-status")).to_have_text("邮件处理结果已汇报")
    expect(page.locator("#assistant-messages")).to_contain_text("已处理 2 封邮件，更新 1 条投递。本次任务")
    expect(page.locator("#assistant-live-run")).to_be_hidden()
    assert [item["action"] for item in fixture.report_actions] == ["claim", "complete"]
    page.evaluate("reportHooks.refreshMailConfirmations()")
    assert len(fixture.stream_calls) == 1
    assert [item["action"] for item in fixture.report_actions] == ["claim", "complete"]
    assert_no_mail_execution(fixture)


def test_report_failure_releases_claim_and_requires_explicit_retry(mail_report):
    fixture, page = mail_report
    fixture.fail_stream = True
    page.evaluate("reportHooks.refreshMailConfirmations()")
    retry = page.get_by_role("button", name="重新汇报邮件处理结果", exact=True)
    expect(retry).to_be_visible()
    expect(page.locator("#assistant-messages")).to_contain_text("Synthetic report model unavailable")
    expect(page.locator("#assistant-live-run")).to_be_hidden()
    assert [item["action"] for item in fixture.report_actions] == ["claim", "release"]
    for _ in range(3):
        page.evaluate("reportHooks.refreshMailConfirmations()")
    assert len(fixture.stream_calls) == 1
    assert len(fixture.report_actions) == 2
    fixture.fail_stream = False
    retry.click()
    expect(page.locator("#assistant-message-status")).to_have_text("邮件处理结果已汇报")
    expect(page.locator("#assistant-live-run")).to_be_hidden()
    assert [item["action"] for item in fixture.report_actions] == ["claim", "release", "claim", "complete"]
    assert len(fixture.stream_calls) == 2
    assert_no_mail_execution(fixture)


def test_report_for_another_thread_never_claims_or_calls_the_model(mail_report):
    fixture, page = mail_report
    fixture.run_thread = "different-thread"
    page.evaluate("reportHooks.refreshMailConfirmations()")
    assert fixture.report_actions == []
    assert fixture.stream_calls == []
    expect(page.locator("#assistant-messages")).not_to_contain_text("确认后的邮件")


def test_empty_completed_sse_releases_instead_of_marking_report_complete(mail_report):
    fixture, page = mail_report
    fixture.empty_stream = True
    page.evaluate("reportHooks.refreshMailConfirmations()")
    expect(page.get_by_role("button", name="重新汇报邮件处理结果", exact=True)).to_be_visible()
    expect(page.locator("#assistant-messages")).to_contain_text("未生成邮件处理摘要")
    expect(page.locator("#assistant-live-run")).to_be_hidden()
    assert [item["action"] for item in fixture.report_actions] == ["claim", "release"]
    assert fixture.pending is True
    page.evaluate("reportHooks.refreshMailConfirmations()")
    assert len(fixture.stream_calls) == 1
    assert_no_mail_execution(fixture)


def test_switching_threads_while_claiming_releases_without_model_execution(mail_report):
    fixture, page = mail_report
    fixture.hold_claim = True
    page.evaluate("reportHooks.refreshMailConfirmations()")
    page.wait_for_function("true")
    assert fixture.pending_claim is not None
    page.evaluate("reportHooks.state.codexThreadId = 'another-thread'")
    pending, payload = fixture.pending_claim
    with page.expect_response(lambda response: response.url.endswith("/report") and response.request.post_data_json["action"] == "release"):
        pending.fulfill(json=payload)
    assert fixture.stream_calls == []
    assert [item["action"] for item in fixture.report_actions] == ["claim", "release"]


def test_claim_owned_by_another_window_does_not_generate_a_duplicate_report(mail_report):
    fixture, page = mail_report
    fixture.allow_claim = False
    page.evaluate("reportHooks.refreshMailConfirmations()")
    page.evaluate("new Promise(resolve => setTimeout(resolve, 0))")
    assert [item["action"] for item in fixture.report_actions] == ["claim"]
    assert fixture.stream_calls == []


def test_history_hides_mail_automatic_report_prompt_and_internal_run_id(mail_report):
    _, page = mail_report
    history = {"id": THREAD, "turns": [{"id": "turn-history", "items": [
        {"id": "automatic-user", "type": "userMessage", "content": f'{REPORT_PREFIX} recruitment_mail_run_status(run_id="{REPORT_RUN}", wait_ms=0)'},
        {"id": "automatic-answer", "type": "agentMessage", "text": f"邮件已处理。{REPORT_RUN}"},
    ]}]}
    messages = page.evaluate("history => reportHooks.codexHistoryMessages(history)", history)
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["body"] == "邮件已处理。本次任务"
