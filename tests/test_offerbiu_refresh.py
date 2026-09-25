import pytest

from packages.discovery.company_registry import CompanySourceRegistry
from packages.discovery.offerbiu_refresh import OfferBiuRefreshService
from packages.storage import Storage
from packages.storage.models import CompanySnapshot
from packages.tools.offerbiu_refresh import OfferBiuSourceRefreshInput, refresh_offerbiu_sources


class _Cookies:
    def clear(self):
        pass


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Session:
    cookies = _Cookies()

    def __init__(self, pages):
        self.pages = pages

    def get(self, _url, *, params, **_kwargs):
        assert dict(params)["seasonYear"] == "2027"
        page = int(dict(params)["page"])
        return _Response(self.pages[page])


def _payload(page, rows):
    return {
        "success": True,
        "data": {
            "page": page,
            "size": 9,
            "totalItems": 2,
            "totalPages": 2,
            "previewLimited": False,
            "items": rows,
        },
    }


def _row(record_id, url):
    return {
        "id": record_id,
        "companyName": f"Company {record_id}",
        "targetYears": [2027],
        "recruitType": "秋招",
        "industryGroupCodes": ["internet-tech"],
        "applyUrl": url,
    }


def test_refresh_registers_only_usable_entries_after_complete_snapshot():
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    service = OfferBiuRefreshService(
        CompanySourceRegistry(storage),
        session=_Session([
            _payload(0, [_row("one", "https://jobs.example.com/campus")]),
            _payload(1, [_row("two", "https://mp.weixin.qq.com/s/test")]),
        ]),
    )

    response = refresh_offerbiu_sources(
        OfferBiuSourceRefreshInput(apply=True, delay_seconds=0), service
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.records_seen == 2
    assert response.data.registered_entries == 1
    assert response.data.new_entries == 1
    assert response.data.excluded_unusable == 1
    assert len(response.data.registered_ids) <= 20
    assert len(service.last_registered_ids) == 1
    assert len(response.data.pending_entries) == 1
    assert response.data.pending_entry_count == 1
    assert response.data.pending_entries_sample_count == 1
    assert response.data.pending_entries_limited is False
    assert response.data.pending_entries[0].company_name == "Company one"
    assert response.data.pending_entries[0].entry_url == "https://jobs.example.com/campus"
    assert CompanySourceRegistry(storage).list_sources()["total"] == 1


def test_incomplete_refresh_never_writes_registry():
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    page = _payload(0, [_row("one", "https://jobs.example.com/campus")])
    page["data"]["previewLimited"] = True
    service = OfferBiuRefreshService(CompanySourceRegistry(storage), session=_Session([page]))

    response = refresh_offerbiu_sources(
        OfferBiuSourceRefreshInput(apply=True, delay_seconds=0), service
    )

    assert response.success is False
    assert response.data is not None and response.data.applied is False
    assert response.data.pending_entry_count is None
    assert CompanySourceRegistry(storage).list_sources()["total"] == 0


@pytest.mark.parametrize("years", [None, [], [2026], ["2027"], "missing"])
def test_year_filtered_refresh_trusts_source_cohort_through_registration(years):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    registry = CompanySourceRegistry(storage)
    row = _row("two", "https://jobs.example.com/other")
    if years == "missing":
        row.pop("targetYears")
    else:
        row["targetYears"] = years
    service = OfferBiuRefreshService(registry, session=_Session([
        _payload(0, [_row("one", "https://jobs.example.com/campus")]),
        _payload(1, [row]),
    ]))
    result = service.refresh(delay_seconds=0)
    assert result["complete"] is True
    assert result["stop_reason"] == "complete"
    assert result["pages_fetched"] == 2
    assert result["records_seen"] == result["registered_entries"] == 2
    assert result["out_of_scope"] == 0
    assert registry.list_sources()["total"] == 2
    assert row.get("targetYears", "missing") == years  # Preserve source evidence.
    storage.engine.dispose()


@pytest.mark.parametrize("field,value", [
    ("recruitType", "春招"),
    ("industryGroupCodes", ["finance"]),
])
def test_verified_request_scope_overrides_stale_row_metadata(field, value):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    registry = CompanySourceRegistry(storage)
    row = _row("two", "https://jobs.example.com/other")
    row.pop("targetYears")
    row[field] = value
    service = OfferBiuRefreshService(registry, session=_Session([
        _payload(0, [_row("one", "https://jobs.example.com/campus")]),
        _payload(1, [row]),
    ]))
    result = service.refresh(delay_seconds=0)
    assert result["complete"] is True
    assert result["stop_reason"] == "complete"
    assert result["applied"] is True
    assert result["out_of_scope"] == 0
    assert registry.list_sources()["total"] == 2
    storage.engine.dispose()


def test_refresh_reports_validated_page_progress_without_early_writes():
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    service = OfferBiuRefreshService(
        CompanySourceRegistry(storage),
        session=_Session([
            _payload(0, [_row("one", "https://jobs.example.com/campus")]),
            _payload(1, [_row("two", "https://jobs.example.com/other")]),
        ]),
    )
    seen = []
    result = service.refresh(apply=False, delay_seconds=0,
                             progress_callback=lambda pages, total, records: seen.append((pages, total, records)))
    assert seen == [(1, 2, 1), (2, 2, 2)]
    assert result["complete"] is True
    assert CompanySourceRegistry(storage).list_sources()["total"] == 0


def test_refresh_links_exact_existing_company_and_excludes_it_from_pending():
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    with storage.write_transaction() as db:
        db.add(CompanySnapshot(
            id="existing-one",
            name="Company one",
            aliases=[],
            campus_url="https://jobs.example.com/campus",
            crawler_key="render",
            integration_status="connected",
            source="historical_import",
            source_ref="historical-company:existing-one",
        ))
    service = OfferBiuRefreshService(
        CompanySourceRegistry(storage),
        session=_Session([
            _payload(0, [_row("one", "https://jobs.example.com/campus")]),
            _payload(1, [_row("two", "https://jobs.example.com/other")]),
        ]),
    )

    response = refresh_offerbiu_sources(
        OfferBiuSourceRefreshInput(apply=True, delay_seconds=0), service
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.linked_existing_entries == 1
    assert [entry.company_name for entry in response.data.pending_entries] == ["Company two"]
    assert response.data.pending_entry_count == 1


def test_refresh_reports_total_separately_from_twenty_entry_sample():
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    rows = [_row(str(index), f"https://company{index}.example.com/campus") for index in range(25)]
    page = _payload(0, rows)
    page["data"].update(size=50, totalItems=25, totalPages=1)
    service = OfferBiuRefreshService(CompanySourceRegistry(storage), session=_Session([page]))

    response = refresh_offerbiu_sources(
        OfferBiuSourceRefreshInput(apply=True, delay_seconds=0), service
    )

    assert response.success is True
    assert response.data.pending_entry_count == 25
    assert response.data.pending_entries_sample_count == len(response.data.pending_entries) == 20
    assert response.data.pending_entries_limited is True
    assert response.data.registered_entries == 25
    assert response.data.registered_ids_sample_count == len(response.data.registered_ids) == 20
    assert response.data.registered_ids_limited is True
