from packages.discovery.company_registry import CompanySourceRegistry
from packages.discovery.offerbiu_registry import import_offerbiu_sources
from packages.storage import Storage
from packages.discovery.oc_capture import classify_oc_destination_url
import pytest


@pytest.mark.parametrize("url,kind", [
    ("https://x.wjx.com/vm/test", "form"),
    ("https://jsj.top/f/test", "form"),
    ("https://doc.weixin.qq.com/smartsheet/test", "form"),
    ("https://open.weixin.qq.com/connect/oauth2/authorize", "login_page"),
    ("https://mp.weixinbridge.com/s/test", "article"),
])
def test_shared_source_entry_filter_handles_offerbiu_variants(url, kind):
    assert classify_oc_destination_url(url)[0] == kind


def test_unusable_sources_are_excluded_and_reconsidered_on_reimport():
    storage = Storage.from_url("sqlite:///:memory:", initialize=True)
    registry = CompanySourceRegistry(storage)
    snapshot = {"source": "offerbiu", "source_url": "https://offerbiu.com/api/recruitment/postings",
                "items": [{"id": "rec-1", "companyName": "Example", "targetYears": [2027],
                           "recruitType": "秋招", "industryGroupCodes": ["internet-tech"],
                           "applyUrl": "https://mp.weixin.qq.com/s/notice"}]}
    result = import_offerbiu_sources(registry, snapshot)
    assert result["retained"] == 0
    assert result["excluded_unusable"] == 1
    assert result["ids"] == []
    assert registry.list_sources()["total"] == 0

    snapshot["items"][0]["applyUrl"] = "https://jobs.example.com/campus"
    retried = import_offerbiu_sources(registry, snapshot)
    assert retried["retained"] == 1
    assert retried["excluded_unusable"] == 0
    assert registry.list_sources()["total"] == 1
    storage.engine.dispose()


def test_filter_and_multiple_entries_have_stable_ids():
    storage = Storage.from_url("sqlite:///:memory:", initialize=True)
    registry = CompanySourceRegistry(storage)
    item = {"id": "rec-1", "companyName": "Example", "targetYears": [2027],
            "recruitType": "秋招", "industryGroupCodes": ["auto-transport-equipment"],
            "applyUrl": ["https://jobs.example.com/a", "https://jobs.example.com/b"]}
    snapshot = {"source": "offerbiu", "items": [item, {**item, "id": "old", "targetYears": [2026]}]}
    first = import_offerbiu_sources(registry, snapshot)
    item["applyUrl"].reverse()
    second = import_offerbiu_sources(registry, snapshot)
    assert set(first["ids"]) == set(second["ids"])
    assert first["retained"] == 2
    assert first["out_of_scope"] == 1
    assert registry.list_sources()["total"] == 2
    storage.engine.dispose()
