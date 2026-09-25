"""Deterministic source-only resilience checks; no live network or business data."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
import requests
from sqlalchemy.exc import OperationalError

from packages.discovery.company_registry import CompanySourceRegistry
from packages.discovery import offerbiu_refresh
from packages.discovery.offerbiu_refresh import OfferBiuCheckpointError, OfferBiuRefreshService, capture_offerbiu_snapshot
from packages.discovery.offerbiu_registry import import_offerbiu_sources
from packages.storage import Storage


def row(key, **changes):
    return {"id": key, "companyName": f"Company {key}", "targetYears": [2027],
            "recruitType": "秋招", "industryGroupCodes": ["internet-tech"],
            "applyUrl": f"https://{key}.example.com/campus", **changes}


def page(index, rows, *, total=2, pages=2, limited=False):
    return {"success": True, "data": {"page": index, "size": 9, "totalItems": total,
            "totalPages": pages, "previewLimited": limited, "items": rows}}


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)


class Session:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.cookies = {}

    def get(self, url, *, params, **kwargs):
        self.calls.append((url, params, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, Response) else Response(response)

    @property
    def page_calls(self):
        return [int(dict(params)["page"]) for _, params, _ in self.calls]


@pytest.fixture
def registry():
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    yield CompanySourceRegistry(storage)
    storage.engine.dispose()


def capture(session, **kwargs):
    return capture_offerbiu_snapshot(session=session, delay_seconds=0, sleeper=lambda _: None, **kwargs)


def test_bad_rows_duplicate_and_conflict_are_isolated_without_mutating_evidence():
    good = row("good", targetYears=[2026], recruitType=None, industryGroupCodes="stale")
    conflict = row("conflict")
    rows = [good, deepcopy(good), None, [], {}, row("missing-name", companyName=""),
            row("bad-urls", applyUrl=[{}]), conflict, {**conflict, "companyName": "Other"}, row("tail")]
    session = Session(page(0, rows, total=7, pages=1))
    result = capture(session)
    assert [item["id"] for item in result["items"]] == ["good", "tail"]
    assert result["items"][0] == good
    assert result["partial"] and result["usable"] and not result["complete"]
    assert result["reason"] == "rows_quarantined"
    assert result["counters"]["duplicate_ids"] == 2
    assert result["counters"]["quarantined_rows"] == 6
    assert result["counters"]["conflicting_ids"] == 1


def test_identical_ids_are_deduplicated_and_counted_against_unique_total():
    result = capture(Session(page(0, [row("a"), row("a")]), page(1, [row("b")])))
    assert result["complete"]
    assert len(result["items"]) == 2
    assert result["counters"]["duplicate_ids"] == 1


def test_verified_selected_request_applies_even_when_row_metadata_disagrees(registry):
    session = Session(page(0, [row("a", targetYears=None, recruitType="春招",
                                   industryGroupCodes=["internet-tech"])], total=1, pages=1))
    service = OfferBiuRefreshService(registry, session=session, scope={"industry_groups": ["finance"]})
    result = service.refresh(delay_seconds=0)
    params = session.calls[0][1]
    assert ("seasonYear", "2027") in params and ("recruitType", "秋招") in params
    assert [value for key, value in params if key == "industryGroup"] == ["finance"]
    assert result["complete"] and result["applied"] and result["out_of_scope"] == 0
    assert registry.list_sources()["total"] == 1


@pytest.mark.parametrize("failure", [requests.Timeout("synthetic"), Response(status=503), Response(status=429)])
def test_retry_exhaustion_preserves_prior_normal_pages(failure):
    session = Session(page(0, [row("a")]), failure, failure, failure)
    sleeps = []
    result = capture_offerbiu_snapshot(session=session, delay_seconds=0, sleeper=sleeps.append)
    assert result["partial"] and result["usable"]
    assert result["reason"].endswith("retry_exhausted")
    assert session.page_calls == [0, 1, 1, 1]
    assert sleeps == [0.25, 0.5]
    assert result["counters"]["requests"] == 4
    assert result["counters"]["retries"] == 2


def test_retry_success_can_finish_complete():
    session = Session(requests.Timeout(), Response(status=502), page(0, [row("a")], total=1, pages=1))
    result = capture(session)
    assert result["complete"]
    assert session.page_calls == [0, 0, 0]


def test_json_parse_failure_retries_same_page_within_budget():
    session = Session(Response(ValueError("truncated JSON")), page(0, [row("a")], total=1, pages=1))
    result = capture(session)
    assert result["complete"] and result["counters"]["retries"] == 1
    assert session.page_calls == [0, 0]


def test_exhausted_json_parse_retries_preserve_normal_pages():
    session = Session(page(0, [row("a")]), *(Response(ValueError("truncated JSON")) for _ in range(3)))
    result = capture(session)
    assert result["reason"] == "invalid_json_retry_exhausted"
    assert result["partial"] and result["usable"]
    assert result["counters"]["retries"] == 2 and session.page_calls == [0, 1, 1, 1]


def test_retry_after_is_respected_within_bounded_wait_budget():
    session = Session(Response(status=429, headers={"Retry-After": "2"}),
                      page(0, [row("a")], total=1, pages=1))
    sleeps = []
    result = capture_offerbiu_snapshot(session=session, delay_seconds=0, sleeper=sleeps.append)
    assert result["complete"] and sleeps == [2.0]


def test_long_retry_after_stops_and_saves_progress_instead_of_retrying_early(tmp_path):
    checkpoint = tmp_path / "progress.json"
    session = Session(page(0, [row("a")]), Response(status=429, headers={"Retry-After": "60"}))
    sleeps = []
    result = capture_offerbiu_snapshot(session=session, delay_seconds=0, sleeper=sleeps.append,
                                      checkpoint_path=checkpoint)
    assert result["reason"] == "retry_after_exceeds_budget"
    assert result["partial"] and result["usable"] and result["retry_after_seconds"] == 60
    assert session.page_calls == [0, 1] and sleeps == []
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["next_page"] == 1


@pytest.mark.parametrize("failure,reason", [
    (Response(status=401), "http_401"), (Response(status=403), "http_403"),
    (Response(status=302), "http_302"),
    (page(0, [row("hidden")], limited=True), "preview_or_access_limit"),
    ({"success": True, "data": []}, "invalid_or_restricted_response"),
])
def test_restricted_pages_are_never_retried_or_registered(registry, failure, reason):
    session = Session(failure)
    service = OfferBiuRefreshService(registry, session=session)
    result = service.refresh(delay_seconds=0)
    assert result["reason"] == reason
    assert result["partial"] and not result["usable"] and not result["applied"]
    assert len(session.calls) == 1
    assert registry.list_sources()["total"] == 0


def test_limited_page_never_enters_partial_normal_registry(registry):
    service = OfferBiuRefreshService(registry, session=Session(
        page(0, [row("normal")]), page(1, [row("hidden")], limited=True),
    ))
    result = service.refresh(delay_seconds=0)
    assert result["partial"] and result["usable"] and result["applied"]
    assert result["registered_entries"] == 1
    assert registry.list_sources()["items"][0]["company_name"] == "Company normal"


def test_partial_refresh_adds_valid_rows_and_keeps_prior_sources(registry):
    old = import_offerbiu_sources(registry, {"source": "offerbiu", "items": [row("old")]})
    service = OfferBiuRefreshService(registry, session=Session(page(0, [row("new")]), requests.Timeout()))
    result = service.refresh(delay_seconds=0, max_retries=0)
    assert result["partial"] and result["applied"]
    assert result["registered_entries"] == 1
    assert registry.get_source(old["ids"][0]) is not None
    assert registry.list_sources()["total"] == 2


def test_total_changes_trigger_bounded_overlap_and_remain_partial():
    session = Session(page(0, [row("a")]),
                      page(1, [row("b")], total=3, pages=3),
                      page(0, [row("a")], total=4, pages=3),
                      page(0, [row("a")], total=5, pages=3),
                      page(2, [row("c")], total=5, pages=3))
    result = capture(session, max_pages=10, max_overlap_pages=2)
    assert result["reason"] == "source_changed_during_capture"
    assert result["partial"] and result["usable"]
    assert session.page_calls == [0, 1, 0, 0, 2]
    assert result["counters"]["total_changes"] == 3
    assert result["expected_total"] == 5
    assert len(result["items"]) == 3


def test_page_budget_bounds_overlap_requests():
    session = Session(page(0, [row("a")]), page(1, [row("b")], total=3, pages=3))
    result = capture(session, max_pages=2)
    assert result["reason"] == "page_budget_exhausted"
    assert len(session.calls) == 2
    assert result["partial"]


def test_checkpoint_continues_with_overlap_and_id_deduplication(tmp_path):
    checkpoint = tmp_path / "progress.json"
    first = capture(Session(page(0, [row("a")]), requests.Timeout()),
                    checkpoint_path=checkpoint, max_retries=0)
    assert first["partial"]
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["next_page"] == 1 and not saved["complete"]
    assert saved["pages"][0]["totalItems"] == 2
    assert saved["pages"][0]["previewLimited"] is False
    session = Session(page(0, [row("a")]), page(1, [row("b")]))
    second = capture(session, checkpoint_path=checkpoint)
    assert second["complete"] and second["resumed"]
    assert second["counters"]["resumed_records"] == 1
    assert second["counters"]["resumed_pages"] == 1
    assert second["counters"]["fresh_records"] == 2
    assert second["counters"]["duplicate_ids"] == 1
    assert session.page_calls == [0, 1]
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["complete"]
    assert not list(tmp_path.glob("*.tmp"))


def test_completed_checkpoint_is_not_reused_as_new_fresh_snapshot(tmp_path):
    checkpoint = tmp_path / "progress.json"
    capture(Session(page(0, [row("old")], total=1, pages=1)), checkpoint_path=checkpoint)
    session = Session(Response(status=403))
    result = capture(session, checkpoint_path=checkpoint)
    assert not result["resumed"] and not result["fresh"] and not result["usable"]
    assert result["items"] == []
    assert result["counters"]["resumed_records"] == result["counters"]["fresh_records"] == 0
    assert session.page_calls == [0]


def test_expired_checkpoint_starts_at_zero_without_old_rows(tmp_path):
    checkpoint = tmp_path / "progress.json"
    capture(Session(page(0, [row("old")])), checkpoint_path=checkpoint, max_pages=1)
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    saved["started_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    checkpoint.write_text(json.dumps(saved), encoding="utf-8")
    result = capture(Session(page(0, [row("new")], total=1, pages=1)), checkpoint_path=checkpoint)
    assert result["complete"] and not result["resumed"]
    assert [item["id"] for item in result["items"]] == ["new"]


def test_checkpoint_scope_mismatch_preserves_old_evidence_and_restarts_requested_scope(tmp_path):
    checkpoint = tmp_path / "progress.json"
    capture(Session(page(0, [row("old")])), checkpoint_path=checkpoint, max_pages=1)
    session = Session(page(0, [row("new")], total=1, pages=1))
    result = capture(session, checkpoint_path=checkpoint, scope={"industry_groups": ["finance"]})
    assert result["complete"] and not result["resumed"]
    assert "scope mismatch" in result["checkpoint_warning"]
    assert result["counters"]["checkpoint_resets"] == 1
    assert session.page_calls == [0]
    assert [item["id"] for item in result["items"]] == ["new"]
    assert len(list(tmp_path.glob("*.invalid-*.json"))) == 1


@pytest.mark.parametrize("field,value", [("counters", []), ("pages", [{}]), ("items", [None])])
def test_corrupt_checkpoint_is_preserved_and_restarted_safely(tmp_path, field, value):
    checkpoint = tmp_path / "progress.json"
    capture(Session(page(0, [row("old")])), checkpoint_path=checkpoint, max_pages=1)
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    saved[field] = value
    checkpoint.write_text(json.dumps(saved), encoding="utf-8")
    session = Session(page(0, [row("new")], total=1, pages=1))
    result = capture(session, checkpoint_path=checkpoint)
    assert result["complete"] and not result["resumed"]
    assert result["checkpoint_warning"].startswith("invalid_checkpoint")
    assert result["counters"]["checkpoint_resets"] == 1 and session.page_calls == [0]
    assert [item["id"] for item in result["items"]] == ["new"]
    backup = list(tmp_path.glob("*.invalid-*.json"))
    assert len(backup) == 1
    assert json.loads(backup[0].read_text(encoding="utf-8"))[field] == value


def test_traversed_partial_checkpoint_does_not_perpetuate_old_conflicts(tmp_path):
    checkpoint = tmp_path / "progress.json"
    first = capture(Session(page(0, [row("a"), row("a", companyName="Ambiguous"), row("b")], pages=1)),
                    checkpoint_path=checkpoint)
    assert first["partial"] and first["counters"]["conflicting_ids"] == 1
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert not saved["complete"] and saved["traversal_finished"]
    second = capture(Session(page(0, [row("a"), row("b")], pages=1)), checkpoint_path=checkpoint)
    assert second["complete"] and not second["resumed"]
    assert second["counters"]["conflicting_ids"] == second["counters"]["quarantined_rows"] == 0


def test_checkpoint_atomic_replace_retries_transient_permission_error(tmp_path, monkeypatch):
    checkpoint = tmp_path / "progress.json"
    original = offerbiu_refresh.os.replace
    calls = []
    sleeps = []

    def transient(source, destination):
        calls.append((source, destination))
        if len(calls) < 3:
            raise PermissionError("Synthetic Windows file lock")
        return original(source, destination)

    monkeypatch.setattr(offerbiu_refresh.os, "replace", transient)
    offerbiu_refresh._write_checkpoint(checkpoint, {"valid": True}, sleeper=sleeps.append)
    assert len(calls) == 3 and sleeps == [0.05, 0.1]
    assert json.loads(checkpoint.read_text(encoding="utf-8")) == {"valid": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_persistent_checkpoint_failure_is_explicit_and_keeps_previous_file(tmp_path, monkeypatch):
    checkpoint = tmp_path / "progress.json"
    offerbiu_refresh._write_checkpoint(checkpoint, {"previous": True})
    calls = []

    def locked(source, destination):
        calls.append((source, destination))
        raise PermissionError("Synthetic persistent file lock")

    monkeypatch.setattr(offerbiu_refresh.os, "replace", locked)
    with pytest.raises(OfferBiuCheckpointError, match="replacement failed"):
        offerbiu_refresh._write_checkpoint(checkpoint, {"new": True}, sleeper=lambda _: None)
    assert len(calls) == 3
    assert json.loads(checkpoint.read_text(encoding="utf-8")) == {"previous": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_registry_rejects_explicitly_restricted_snapshot(registry):
    with pytest.raises(ValueError, match="Restricted"):
        import_offerbiu_sources(registry, {"source": "offerbiu", "previewLimited": True,
                                          "items": [row("preview")]})
    assert registry.list_sources()["total"] == 0


def test_registry_quarantines_bad_boundaries_and_unfiltered_scope_stays_strict(registry):
    valid = row("valid")
    result = import_offerbiu_sources(registry, {"source": "offerbiu", "items": [
        None, {}, row("long", companyName="x" * 256), row("url", applyUrl=[{}]),
        row("old", targetYears=[2026]), row("spring", recruitType="春招"),
        row("industry", industryGroupCodes={}), valid, deepcopy(valid), row("tail"),
    ]})
    assert result["retained"] == 2
    assert result["quarantined_rows"] == 4
    assert result["out_of_scope"] == 3
    assert result["duplicate_ids"] == 1
    assert registry.list_sources()["total"] == 2


def test_registry_conflicting_id_is_quarantined_before_any_write(registry):
    result = import_offerbiu_sources(registry, {"source": "offerbiu", "items": [
        row("conflict"), row("conflict", companyName="Other"), row("good"),
    ]})
    assert result["conflicting_ids"] == result["quarantined_rows"] == 1
    assert result["retained"] == 1
    assert registry.list_sources()["items"][0]["company_name"] == "Company good"


def test_registry_data_boundary_error_does_not_cancel_other_rows(registry, monkeypatch):
    original = registry.upsert_source

    def guarded(**kwargs):
        if kwargs["company_name"] == "Company rejected":
            raise ValueError("Synthetic row validation failure")
        return original(**kwargs)

    monkeypatch.setattr(registry, "upsert_source", guarded)
    result = import_offerbiu_sources(registry, {"source": "offerbiu", "items": [
        row("first"), row("rejected"), row("last"),
    ]})
    assert result["retained"] == 2 and result["quarantined_rows"] == 1


def test_database_error_propagates_and_preserves_committed_registration(registry, monkeypatch):
    original = registry.upsert_source

    def broken(**kwargs):
        if kwargs["company_name"] == "Company second":
            raise OperationalError("synthetic database failure", None, Exception("unavailable"))
        return original(**kwargs)

    monkeypatch.setattr(registry, "upsert_source", broken)
    service = OfferBiuRefreshService(registry, session=Session(
        page(0, [row("first"), row("second")], pages=1),
    ))
    with pytest.raises(OperationalError):
        service.refresh(delay_seconds=0)
    assert len(service.last_registered_ids) == 1
    assert registry.get_source(service.last_registered_ids[0])["company_name"] == "Company first"
    assert registry.list_sources()["total"] == 1
