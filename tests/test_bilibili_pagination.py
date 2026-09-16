from __future__ import annotations

import copy

from packages.recruitment_core.crawlers import bilibili as bilibili_module


API_URL = "https://jobs.bilibili.com/api/campus/position/positionList"
DEFAULT_SCOPE = {"pageSize": 10, "keyword": ""}


def _row(job_id: str) -> dict:
    return {"id": job_id, "positionName": f"Engineer {job_id}"}


def _payload(ids: list[str], page: int, *, total: int = 92, total_pages: int = 10) -> dict:
    return {
        "data": {
            "list": [_row(job_id) for job_id in ids],
            "totalCount": total,
            "currentPage": page,
            "pageSize": 10,
            "totalPages": total_pages,
        }
    }


def _page_ids(page: int, *, total_rows: int = 92) -> list[str]:
    start = (page - 1) * 10 + 1
    return [f"job-{number}" for number in range(start, min(start + 10, total_rows + 1))]


class _Request:
    def __init__(self, body: dict):
        self.post_data_json = copy.deepcopy(body)


class _Response:
    def __init__(self, body: dict, payload: dict):
        self.request = _Request(body)
        self.payload = copy.deepcopy(payload)
        self.url = API_URL

    def json(self) -> dict:
        return copy.deepcopy(self.payload)


class _Locator:
    def __init__(self, page: "_Page", index: int, *, visible: bool, disabled: bool):
        self.page = page
        self.index = index
        self.visible = visible
        self.disabled = disabled

    def is_visible(self) -> bool:
        return self.visible

    def get_attribute(self, name: str) -> str | None:
        if name == "class":
            return "ant-pagination-next disabled" if self.disabled else "ant-pagination-next"
        if name == "disabled":
            return "" if self.disabled else None
        if name == "aria-disabled":
            return "true" if self.disabled else "false"
        return None

    def click(self, **_kwargs) -> None:
        self.page.click_targets.append(self.index)
        self.page.advance()


class _LocatorSet:
    def __init__(self, page: "_Page", *, disabled: bool):
        self.locators = [
            _Locator(page, 0, visible=False, disabled=disabled),
            _Locator(page, 1, visible=True, disabled=disabled),
        ]

    def count(self) -> int:
        return len(self.locators)

    def nth(self, index: int) -> _Locator:
        return self.locators[index]


class _Expectation:
    def __init__(self, page: "_Page", predicate):
        self.page = page
        self.predicate = predicate
        self.matched = None

    def __enter__(self):
        self.page.expectation = self
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.page.expectation = None
        if exc_type is None and self.matched is None:
            raise TimeoutError("fixture did not receive the expected response")
        return False


class _Page:
    def __init__(self, fixture: "_ResponseFixture"):
        self.fixture = fixture
        self.current_page = 1
        self.handlers = []
        self.expectation: _Expectation | None = None
        self.click_targets = []
        self.matched_pages = []
        self.emitted = []

    def on(self, event: str, handler) -> None:
        assert event == "response"
        self.handlers.append(handler)

    def goto(self, *_args, **_kwargs) -> None:
        for response in self.fixture.initial_responses():
            self.emit(response)

    def wait_for_selector(self, *_args, **_kwargs) -> None:
        return None

    def wait_for_timeout(self, *_args, **_kwargs) -> None:
        return None

    def content(self) -> str:
        return "<main></main>"

    def locator(self, selector: str) -> _LocatorSet:
        assert selector == bilibili_module.BilibiliCrawler.NEXT_PAGE_SELECTOR
        return _LocatorSet(self, disabled=self.fixture.next_disabled(self.current_page))

    def expect_response(self, predicate, **_kwargs) -> _Expectation:
        return _Expectation(self, predicate)

    def emit(self, response: _Response) -> None:
        self.emitted.append(response)
        for handler in self.handlers:
            handler(response)
        if self.expectation is not None:
            try:
                matched = self.expectation.predicate(response)
            except Exception:
                matched = False
            if matched and self.expectation.matched is None:
                self.expectation.matched = response
                self.matched_pages.append(response.request.post_data_json["pageNum"])

    def advance(self) -> None:
        next_page = self.current_page + 1
        for response in self.fixture.click_responses(self.current_page):
            self.emit(response)
        self.current_page = next_page


class _ResponseFixture:
    def __init__(
        self,
        *,
        total_rows: int = 92,
        page_count: int = 10,
        scope: dict | None = None,
        include_decoys: bool = True,
    ):
        self.total_rows = total_rows
        self.page_count = page_count
        self.scope = copy.deepcopy(DEFAULT_SCOPE if scope is None else scope)
        self.include_decoys = include_decoys
        self.page = _Page(self)

    def _response(self, page: int, ids: list[str], *, scope: dict | None = None) -> _Response:
        body = dict(self.scope if scope is None else scope)
        body["pageNum"] = page
        return _Response(
            body,
            _payload(ids, page, total=92, total_pages=10),
        )

    def initial_responses(self) -> list[_Response]:
        responses = []
        if self.include_decoys:
            responses.append(
                self._response(52, [f"invalid-52-{number}" for number in range(92)])
            )
            responses.append(
                self._response(92, [f"invalid-92-{number}" for number in range(2)])
            )
        responses.append(self._response(1, _page_ids(1, total_rows=self.total_rows)))
        if self.include_decoys:
            wrong_scope = dict(self.scope)
            wrong_scope["keyword"] = "wrong-scope"
            responses.append(self._response(2, ["wrong-scope-2"], scope=wrong_scope))
        return responses

    def click_responses(self, current_page: int) -> list[_Response]:
        next_page = current_page + 1
        valid_ids = _page_ids(next_page, total_rows=self.total_rows)
        responses = []
        if self.include_decoys:
            if next_page + 1 <= 10:
                responses.append(
                    self._response(next_page + 1, [f"prefetch-{next_page + 1}"])
                )
            wrong_scope = dict(self.scope)
            wrong_scope["keyword"] = "wrong-scope"
            responses.append(self._response(next_page, [f"wrong-{next_page}"], scope=wrong_scope))
        responses.append(self._response(next_page, valid_ids))
        return responses

    def next_disabled(self, current_page: int) -> bool:
        return current_page >= self.page_count


class _Context:
    def __init__(self, page: _Page):
        self.page = page

    def new_page(self) -> _Page:
        return self.page

    def close(self) -> None:
        return None


class _Browser:
    def __init__(self, page: _Page):
        self.context = _Context(page)

    def new_context(self, **_kwargs) -> _Context:
        return self.context

    def close(self) -> None:
        return None


class _SyncPlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, *_args) -> None:
        return None


def _crawler(monkeypatch, fixture: _ResponseFixture):
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: _SyncPlaywright(),
    )
    monkeypatch.setattr(
        bilibili_module,
        "launch_browser",
        lambda *_args, **_kwargs: _Browser(fixture.page),
    )
    return bilibili_module.BilibiliCrawler("bilibili", bilibili_module.BilibiliCrawler.LIST_URL)


def test_bilibili_accepts_only_scoped_pages_and_binds_visible_next_to_expected_response(monkeypatch):
    fixture = _ResponseFixture()
    crawler = _crawler(monkeypatch, fixture)

    jobs = crawler.fetch()

    job_ids = {job["jd_url"].rsplit("/", 1)[-1] for job in jobs}
    assert job_ids == {f"job-{number}" for number in range(1, 93)}
    assert crawler.advertised_total == 92
    assert crawler.total_pages == crawler.pages_seen == 10
    assert crawler.pagination_complete is True
    assert crawler.has_more is False
    assert fixture.page.click_targets == [1] * 9
    assert fixture.page.matched_pages == list(range(2, 11))


def test_bilibili_does_not_mark_52_of_92_complete(monkeypatch):
    fixture = _ResponseFixture(total_rows=52, page_count=6, include_decoys=False)
    crawler = _crawler(monkeypatch, fixture)

    jobs = crawler.fetch()

    assert len(jobs) == 52
    assert crawler.advertised_total == 92
    assert crawler.pagination_complete is False
    assert crawler.has_more is True
    assert crawler.pagination_termination_reason == "next_control_disabled_before_total"


def test_bilibili_locks_an_empty_scope_instead_of_adopting_a_later_scope(monkeypatch):
    fixture = _ResponseFixture(scope={}, include_decoys=True)
    crawler = _crawler(monkeypatch, fixture)

    jobs = crawler.fetch()

    assert {job["title"] for job in jobs} == {
        f"Engineer job-{number}" for number in range(1, 93)
    }
    assert crawler.pagination_complete is True
    assert fixture.page.matched_pages == list(range(2, 11))


def test_bilibili_next_selector_excludes_jump_and_numbered_page_classes() -> None:
    selector = bilibili_module.BilibiliCrawler.NEXT_PAGE_SELECTOR

    assert "[class*=" not in selector
    assert "ant-pagination-jump-next" not in selector
    assert ".ant-pagination-next" in selector
