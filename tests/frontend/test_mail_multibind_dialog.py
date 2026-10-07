"""Offline multi-application mail confirmation; no real mail or business writes."""

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from test_mail_confirmation_dialog import DIGEST, THREAD, MailDialogFixture


class MultiBindingFixture(MailDialogFixture):
    def __init__(self):
        super().__init__()
        self.multiple = True
        self.current_ids = []
        self.binding_revision = 0

    @staticmethod
    def candidate(index):
        return {"application_id": f"application-{index}", "company_name": "示例科技",
                "job_title": f"开发岗位 {index}", "stage": "interview1" if index == 1 else "applied",
                "city": "深圳", "reason": "company_candidate", "selectable": True,
                "recommended": index < 2}

    def route(self, route):
        url = urlsplit(route.request.url)
        if url.path.endswith("/binding-candidates"):
            assert url.netloc == "ui.example.test"
            query = parse_qs(url.query)
            self.calls.append((route.request.method, url.path, query, None))
            offset = int(query.get("offset", [0])[0])
            search = query.get("query", [""])[0]
            candidates = [self.candidate(index) for index in range(25)]
            if search:
                candidates = [candidate for candidate in candidates if search in candidate["job_title"]]
            if not self.multiple:
                for candidate in candidates:
                    candidate["selectable"] = candidate["application_id"] == "application-0"
            current = [self.candidate(int(identifier.split("-")[-1])) for identifier in self.current_ids]
            return route.fulfill(json={
                "record_id": url.path.split("/")[3], "subject": "示例科技校园招聘统一笔试",
                "excerpt": "同一次笔试适用于本公司的多个投递。", "content_digest": self.candidate_digest,
                "binding_revision": self.binding_revision, "current_application_ids": self.current_ids,
                "current_applications": current, "current_application_id": self.current_ids[0] if self.current_ids else None,
                "allows_multiple": self.multiple, "selection_scope": "company_event" if self.multiple else "job_specific",
                "recommended_application_ids": ["application-0", "application-1"],
                "candidates": candidates[offset:offset + 20], "total": len(candidates),
                "has_more": offset + 20 < len(candidates),
            })
        return super().route(route)


@pytest.fixture
def multi_dialog():
    fixture = MultiBindingFixture()
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        context.route("**/*", fixture.route)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://ui.example.test/", wait_until="networkidle")
        page.evaluate(f"mailDialogHooks.state.codexThreadId = '{THREAD}'")
        page.locator('[data-view="assistant"]').click()
        page.evaluate("mailDialogHooks.refreshMailConfirmations()")
        yield fixture, page, page.locator("#mail-binding-dialog")
        assert not errors, errors
        browser.close()


def open_multi(page, dialog):
    page.locator("#assistant-mail-binding-approvals").get_by_role("button").click()
    expect(dialog).to_be_visible()


def test_multiselect_is_explicit_and_executes_one_approval_and_one_continuation(multi_dialog):
    fixture, page, dialog = multi_dialog
    open_multi(page, dialog)
    expect(dialog.get_by_role("checkbox")).to_have_count(20)
    expect(dialog.locator("input:checked")).to_have_count(0)
    expect(dialog.get_by_role("button", name="确认关联并继续处理", exact=True)).to_be_disabled()
    assert not fixture.mutations()
    dialog.get_by_role("button", name="选择本页推荐岗位", exact=True).click()
    expect(dialog).to_contain_text("已选择 2 条投递")
    expect(dialog).to_contain_text("不回退更高阶段")
    expect(dialog).to_contain_text("只生成一条日程")
    dialog.get_by_role("button", name="确认关联并继续处理", exact=True).evaluate("button => { button.click(); button.click(); }")
    expect(dialog).to_contain_text("第 1 / 1 封")
    calls = fixture.mutations()
    assert [call[1].rsplit("/", 1)[1] for call in calls] == ["binding-proposals", "approve", "execute", "resolve"]
    assert calls[0][3]["application_ids"] == ["application-0", "application-1"]
    assert calls[0][3]["application_id"] is None
    expect(dialog.locator("input:checked")).to_have_count(0)


def test_selected_jobs_survive_pagination_and_search_and_can_be_removed(multi_dialog):
    fixture, page, dialog = multi_dialog
    open_multi(page, dialog)
    dialog.get_by_role("checkbox").nth(1).check()
    dialog.get_by_role("button", name="下一页", exact=True).click()
    expect(dialog.get_by_role("checkbox")).to_have_count(5)
    expect(dialog).to_contain_text("已选择 1 条投递")
    dialog.get_by_role("checkbox").first.check()
    expect(dialog).to_contain_text("已选择 2 条投递")
    dialog.get_by_role("searchbox").fill("开发岗位 3")
    dialog.get_by_role("button", name="搜索", exact=True).click()
    expect(dialog.get_by_role("checkbox")).to_have_count(1)
    expect(dialog).to_contain_text("已选择 2 条投递")
    dialog.get_by_role("button", name="移除关联：开发岗位 1", exact=True).click()
    expect(dialog).to_contain_text("已选择 1 条投递")
    expect(dialog.get_by_role("button", name="确认关联并继续处理", exact=True)).to_be_enabled()
    assert not fixture.mutations()


def test_reopen_restores_all_confirmed_jobs_including_off_page_selection(multi_dialog):
    fixture, page, dialog = multi_dialog
    fixture.current_ids = ["application-0", "application-21"]
    open_multi(page, dialog)
    expect(dialog).to_contain_text("已选择 2 条投递")
    expect(dialog.locator(".mail-binding-selected")).to_contain_text("开发岗位 21")
    expect(dialog.locator("input:checked")).to_have_count(1)
    dialog.get_by_role("button", name="稍后处理", exact=True).click()
    open_multi(page, dialog)
    expect(dialog).to_contain_text("已选择 2 条投递")
    assert not fixture.mutations()


def test_job_specific_mail_keeps_single_selection_and_disables_other_roles(multi_dialog):
    fixture, page, dialog = multi_dialog
    fixture.multiple = False
    open_multi(page, dialog)
    expect(dialog.get_by_role("checkbox")).to_have_count(0)
    expect(dialog.get_by_role("radio")).to_have_count(20)
    expect(dialog.get_by_role("radio").nth(1)).to_be_disabled()
    expect(dialog).to_contain_text("岗位专属通知")
    dialog.get_by_role("radio").first.check()
    dialog.get_by_role("button", name="确认关联并继续处理", exact=True).click()
    expect(dialog).to_contain_text("第 1 / 1 封")
    assert fixture.mutations()[0][3]["application_ids"] == ["application-0"]
    assert fixture.mutations()[0][3]["application_id"] == "application-0"


def test_resume_failure_does_not_repeat_multi_binding_execution(multi_dialog):
    fixture, page, dialog = multi_dialog
    fixture.fail_resolve = True
    open_multi(page, dialog)
    dialog.get_by_role("checkbox").first.check()
    dialog.get_by_role("checkbox").nth(1).check()
    dialog.get_by_role("button", name="确认关联并继续处理", exact=True).click()
    expect(dialog.get_by_role("alert")).to_contain_text("关联已保存")
    expect(dialog.get_by_role("checkbox").first).to_be_disabled()
    fixture.fail_resolve = False
    dialog.get_by_role("button", name="重试续办", exact=True).click()
    expect(dialog).to_contain_text("第 1 / 1 封")
    assert fixture.proposals == 1
    assert sum(call[1].endswith("/execute") for call in fixture.mutations()) == 1
    assert sum(call[1].endswith("/resolve") for call in fixture.mutations()) == 2


def test_multi_dialog_mobile_keeps_selection_and_confirmation_reachable(multi_dialog):
    fixture, page, dialog = multi_dialog
    page.set_viewport_size({"width": 390, "height": 844})
    open_multi(page, dialog)
    dialog.get_by_role("checkbox").first.check()
    expect(dialog.get_by_role("button", name="确认关联并继续处理", exact=True)).to_be_enabled()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not fixture.mutations()


def test_multi_selection_clears_when_confirmed_binding_changes_elsewhere(multi_dialog):
    fixture, page, dialog = multi_dialog
    open_multi(page, dialog)
    dialog.get_by_role("checkbox").first.check()
    dialog.get_by_role("checkbox").nth(1).check()
    fixture.binding_revision = 1
    fixture.current_ids = ["application-2"]
    dialog.get_by_role("button", name="下一页", exact=True).click()
    expect(dialog).to_contain_text("关联已变化，请重新选择")
    expect(dialog).to_contain_text("已选择 0 条投递")
    expect(dialog.get_by_role("button", name="确认关联并继续处理", exact=True)).to_be_disabled()
    assert not fixture.mutations()
