from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error

from packages.recruitment_core.crawlers.tplink import HOME, TPLinkCrawler
from packages.recruitment_core.runner import _crawler_evidence


@pytest.mark.parametrize("exception_type", [Error, ValueError])
def test_tplink_failure_has_stable_code_and_full_transportable_details(monkeypatch, exception_type):
    message = "browser unavailable\n" * 1000 + "END OF DIAGNOSTIC"
    runtime = MagicMock()
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: runtime)

    def fail(*args, **kwargs):
        raise exception_type(message)

    monkeypatch.setattr("packages.recruitment_core.crawlers.tplink.launch_browser", fail)
    crawler = TPLinkCrawler("TP-LINK", HOME)
    assert crawler.fetch() == []
    assert crawler.fetch_failed is True
    assert crawler.pagination_complete is False
    assert crawler.pagination_termination_reason == "tplink_fetch_failed"
    assert crawler.crawl_error_code == "tplink_fetch_failed"
    assert message in crawler.failure_reason
    evidence = _crawler_evidence(crawler, 0, HOME)
    assert evidence["error_code"] == "tplink_fetch_failed"
    assert message in evidence["pagination_diagnostics"][0]["reason"]
    crawler._reset()
    assert crawler.failure_reason == ""
    assert crawler.crawl_error_code == ""
    assert crawler.pagination_diagnostics == []
    assert crawler.fetch_failed is False
