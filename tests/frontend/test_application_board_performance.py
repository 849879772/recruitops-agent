"""Offline board regressions: bounded DOM work, retained cards and lazy editors."""

from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright


WEB = Path(__file__).resolve().parents[2] / "apps" / "web"


def records(count):
    return [
        {"id": f"application-{index}", "company_name": f"Company {index}",
         "job_title": f"Engineer {index}", "stage": "applied",
         "updated_at": "2026-09-29T00:00:00Z", "record_url": None,
         "stage_history": [{"stage": "applied", "date": "2026-09-28", "source": "recruitment_mail"}],
         "last_review": {"state": "unchanged", "checked_at": "2026-09-29T00:00:00Z"}}
        for index in range(count)
    ]


class BoardFixture:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.writes = []
        self.fail_writes = False
        self.fail_pages = False

    def route(self, route):
        url = urlsplit(route.request.url)
        assert url.netloc == "ui.example.test", "Live network requests are forbidden"
        filename = url.path.lstrip("/") or "index.html"
        if filename in {"index.html", "app.js", "configuration.js", "company-sources.js", "styles.css"}:
            source = (WEB / filename).read_text(encoding="utf-8")
            if filename == "app.js":
                source = source.replace("function renderApplications(payload = null) {",
                                        "function renderApplications(payload = null) { const renderStart = performance.now(); try {")
                source = source.replace('kanban.dataset.state = "ready";\n  }',
                                        'kanban.dataset.state = "ready";\n } finally { (globalThis.boardRenderTimes ||= []).push(performance.now() - renderStart); }\n  }')
                source = source.replace("if (globalThis.__RECRUITOPS_TEST_MODE__) {",
                                        "globalThis.boardHooks = testHooks; if (globalThis.__RECRUITOPS_TEST_MODE__) {")
            mime = "text/html" if filename.endswith("html") else "text/css" if filename.endswith("css") else "application/javascript"
            return route.fulfill(body=source, content_type=mime)
        if url.path == "/api/applications/page":
            if self.fail_pages:
                return route.fulfill(status=503, json={"detail": "Synthetic read failure"})
            params = parse_qs(url.query)
            self.calls.append(params)
            query = params.get("query", [""])[0].casefold()
            matches = [row for row in self.rows if query in f"{row['company_name']} {row['job_title']}".casefold()]
            found = [row for row in matches if row["stage"] in params.get("stages", [])]
            offset, limit = int(params.get("offset", [0])[0]), int(params.get("limit", [50])[0])
            return route.fulfill(json={"items": found[offset:offset + limit], "total": len(found),
                                       "unfiltered_total": len(self.rows), "stage_counts": dict(Counter(row["stage"] for row in matches))})
        if url.path.startswith("/api/local-ui/applications/"):
            if self.fail_writes:
                return route.fulfill(status=409, json={"detail": "Synthetic version conflict"})
            body = route.request.post_data_json
            self.writes.append((route.request.method, url.path, body))
            row = next(row for row in self.rows if row["id"] == url.path.split("/")[4])
            if route.request.method == "DELETE":
                self.rows.remove(row)
            else:
                row.update({key: value for key, value in body.items() if key != "expected_updated_at"})
                row["updated_at"] = "2026-09-29T01:00:00Z"
            return route.fulfill(json={"status": "updated"})
        if url.path == "/api/local-ui/events":
            self.writes.append((route.request.method, url.path, route.request.post_data_json))
            return route.fulfill(json={"status": "created"})
        if url.path == "/health":
            return route.fulfill(json={"status": "ok", "mode": "read_only"})
        if url.path == "/api/codex/health":
            return route.fulfill(json={"enabled": False, "ready": False})
        if url.path in {"/api/schedule", "/api/companies", "/api/approvals", "/api/codex/traces"}:
            return route.fulfill(json=[])
        return route.fulfill(status=503, json={"detail": "Offline fixture: unavailable"})


def assert_board_layout(page, *, empty=False):
    layout = page.locator("#application-kanban").evaluate("""board => ({
      children: [...board.children].map(node => node.className),
      columns: [...board.querySelectorAll(':scope > .kanban-column')].map(node => {
        const bounds = node.getBoundingClientRect();
        return {x: bounds.x, y: bounds.y};
      }),
      left: board.getBoundingClientRect().x,
    })""")
    expected = [f"kanban-column kanban-column--{key}"
                for key in ["applied", "written", "interview", "offer", "closed"]]
    assert layout["children"] == (["application-board-empty"] if empty else []) + expected
    # Counts alone miss an extra grid child shifting every column and wrapping "closed".
    assert abs(layout["columns"][0]["x"] - layout["left"]) < 2
    assert max(column["y"] for column in layout["columns"]) - min(
        column["y"] for column in layout["columns"]) < 2


@pytest.mark.parametrize("width", [1440, 1856])
def test_board_layout_survives_startup_refresh_search_and_channel_changes(width):
    fixture = BoardFixture(records(210))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": width, "height": 900}, service_workers="block")
        context.route("**/*", fixture.route)
        page = context.new_page()
        page.goto("https://ui.example.test/", wait_until="networkidle")
        page.locator('[data-view="applications"]').click()
        expect(page.locator(".application-card")).to_have_count(210)
        assert_board_layout(page)

        page.evaluate("globalThis.originalColumn = document.querySelector('.kanban-column--applied'); "
                      "globalThis.originalCard = document.querySelector('.application-card');")
        page.locator("#applications-refresh-button").click()
        expect(page.locator("#applications-refresh-button")).to_be_enabled()
        assert_board_layout(page)
        assert page.evaluate("originalCard === document.querySelector('.application-card')")

        page.locator("#application-search").fill("No matching record")
        expect(page.locator(".application-card")).to_have_count(0)
        expect(page.locator(".application-board-empty")).to_have_count(1)
        assert_board_layout(page, empty=True)
        page.locator("#application-search").fill("")
        expect(page.locator(".application-card")).to_have_count(210)
        assert_board_layout(page)

        page.locator('[data-application-channel="official_page"]').click()
        expect(page.locator(".application-board-empty")).to_have_count(1)
        assert_board_layout(page, empty=True)
        page.locator('[data-application-channel="all"]').click()
        expect(page.locator(".application-card")).to_have_count(210)
        assert_board_layout(page)
        assert page.evaluate("originalColumn === document.querySelector('.kanban-column--applied')")
        assert not fixture.writes
        browser.close()


@pytest.mark.parametrize("placeholder", ["loading-rows", "error-state", "empty-state"])
def test_board_removes_only_transient_placeholders_on_recovery(placeholder):
    fixture = BoardFixture(records(3))
    fixture.fail_pages = True
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        context.route("**/*", fixture.route)
        page = context.new_page()
        page.add_init_script("globalThis.__RECRUITOPS_TEST_MODE__ = true;")
        page.goto("https://ui.example.test/")
        page.evaluate("""placeholder => {
          document.querySelector('#applications-view').hidden = false;
          document.querySelector('#application-kanban > .loading-rows').className = placeholder;
        }""", placeholder)
        assert page.evaluate("boardHooks.loadApplications()") is False
        assert_board_layout(page)
        fixture.fail_pages = False
        assert page.evaluate("boardHooks.loadApplications()") is True
        expect(page.locator(".application-card")).to_have_count(3)
        assert_board_layout(page)
        # Empty-column messages are nested content, not transient board placeholders.
        expect(page.locator(".kanban-column--written .kanban-empty")).to_have_count(1)
        assert not fixture.writes
        browser.close()


@pytest.mark.parametrize("count", [210, 500, 1000])
def test_large_board_only_builds_changed_cards_and_expanded_history(count):
    fixture = BoardFixture(records(count))
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        context.route("**/*", fixture.route)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("globalThis.__RECRUITOPS_TEST_MODE__ = true;")
        page.goto("https://ui.example.test/")
        page.evaluate("document.querySelector('#applications-view').hidden = false")
        result = page.evaluate("""async () => {
          const started = performance.now();
          await boardHooks.loadApplications();
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          return {elapsed: performance.now() - started, renders: boardRenderTimes,
            nodes: document.querySelector('#application-kanban').querySelectorAll('*').length};
        }""")
        print(f"board {count}: {result}")
        expect(page.locator(".application-card")).to_have_count(count)
        expect(page.locator(".application-editor")).to_have_count(0)
        expect(page.locator(".application-history ol")).to_have_count(0)
        # A structural budget is stable across hosts; wall-clock figures are reported, not a flaky CI gate.
        assert result["nodes"] < count * 35 + 100
        page.locator(".application-history").first.locator("summary").click()
        expect(page.locator(".application-history ol")).to_have_count(1)
        page.evaluate("globalThis.firstCard = document.querySelector('.application-card'); window.scrollTo(0, 500)")
        page.evaluate("boardHooks.loadApplications()")
        assert page.evaluate("firstCard === document.querySelector('.application-card')")
        expect(page.locator(".application-history").first).to_have_attribute("open", "")
        assert page.evaluate("window.scrollY") == 500
        request_count = len(fixture.calls)
        page.evaluate("boardHooks.loadApplications({useCache: true})")
        assert len(fixture.calls) == request_count
        # Only one changed snapshot is rebuilt, while neighboring identity/history is preserved.
        fixture.rows[-1]["note"] = "Changed note"
        page.evaluate("globalThis.lastCard = document.querySelector('.application-card:last-child')")
        page.evaluate("boardHooks.loadApplications()")
        assert page.evaluate("firstCard === document.querySelector('.application-card')")
        assert page.evaluate("lastCard !== document.querySelector('.application-card:last-child')")
        if count == 210:
            fixture.fail_pages = True
            page.locator("#application-search").fill("Engineer 209")
            assert page.evaluate("boardHooks.loadApplications()") is False
            expect(page.locator(".application-card")).to_have_count(count)
            expect(page.locator("#application-page-description")).to_contain_text("搜索加载失败，保留上次结果")
            fixture.fail_pages = False
            page.evaluate("boardHooks.loadApplications()")
            expect(page.locator(".application-card")).to_have_count(1)
            expect(page.locator(".application-card")).to_contain_text("Engineer 209")
        assert not errors
        browser.close()


def test_shared_editor_preserves_draft_during_refresh_and_all_mutations_work():
    fixture = BoardFixture(records(3))
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        context.route("**/*", fixture.route)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("https://ui.example.test/", wait_until="networkidle")
        page.locator('[data-view="applications"]').click()
        card = page.locator('[data-application-id="application-0"]')
        card.get_by_role("button", name="编辑 Company 0 的投递记录", exact=True).click()
        dialog = page.locator("#application-edit-dialog")
        expect(dialog).to_be_visible()
        expect(page.locator(".application-editor")).to_have_count(1)
        dialog.get_by_label("公司名称", exact=True).fill("Renamed Company")
        dialog.get_by_label("岗位名称", exact=True).fill("Renamed Engineer")
        page.evaluate("boardHooks.loadApplications()")
        expect(dialog.get_by_label("岗位名称", exact=True)).to_have_value("Renamed Engineer")
        expect(dialog.get_by_label("岗位名称", exact=True)).to_be_focused()
        fixture.fail_writes = True
        dialog.get_by_role("button", name="更新", exact=True).click()
        expect(page.locator("#toast-region")).to_contain_text("Synthetic version conflict")
        expect(dialog).to_be_visible()
        expect(dialog.get_by_label("公司名称", exact=True)).to_have_value("Renamed Company")
        fixture.fail_writes = False
        dialog.get_by_label("阶段", exact=True).select_option("written")
        dialog.get_by_role("button", name="更新", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(page.locator('.kanban-column--written [data-application-id="application-0"]')).to_have_count(1)
        expect(card).to_contain_text("Renamed Engineer")
        card.get_by_role("button", name="编辑 Renamed Company 的投递记录", exact=True).click()
        dialog.get_by_label("日期", exact=True).fill("2026-09-30")
        dialog.get_by_role("button", name="添加日程", exact=True).click()
        expect(page.locator("#toast-region")).to_contain_text("日程已添加")
        event_body = next(write[2] for write in fixture.writes if write[1] == "/api/local-ui/events")
        assert event_body["application_id"] == "application-0"
        assert event_body["company_name"] == "Renamed Company"
        dialog.get_by_role("button", name="删除投递记录", exact=True).click()
        expect(dialog.get_by_role("group", name="确认删除投递记录")).to_be_visible()
        dialog.get_by_role("button", name="取消", exact=True).click()
        assert not any(write[0] == "DELETE" for write in fixture.writes)
        dialog.get_by_role("button", name="删除投递记录", exact=True).click()
        dialog.get_by_role("button", name="确认删除", exact=True).click()
        expect(dialog).not_to_be_visible()
        expect(card).to_have_count(0)
        expect(page.locator(".application-editor")).to_have_count(0)
        assert sum(write[0] == "DELETE" for write in fixture.writes) == 1
        assert not errors
        browser.close()
