from packages.discovery.company_registry import CompanySourceRegistry
from packages.storage import Storage


def _registry(tmp_path):
    return CompanySourceRegistry(
        Storage.from_url(f"sqlite:///{tmp_path / 'sources.db'}", initialize=True)
    )


def test_upsert_is_source_scoped_and_failures_preserve_success_count(tmp_path):
    registry = _registry(tmp_path)
    first = registry.upsert_source(
        source="oc", source_record_id="42", company_name="同名公司",
        source_url="https://source.example/rows", entry_url="https://jobs.example/a",
    )
    same_key = registry.upsert_source(
        source="oc", source_record_id="42", company_name="更新后的名称",
        source_url="https://source.example/new", entry_url="https://jobs.example/other",
    )
    other_source = registry.upsert_source(
        source="other", source_record_id="42", company_name="更新后的名称",
        source_url="https://other.example/rows", entry_url="https://jobs.example/b",
    )

    assert same_key["id"] == first["id"]
    assert other_source["id"] != first["id"]
    assert same_key["source_url"] == "https://source.example/rows"
    assert same_key["entry_url"] == "https://jobs.example/a"

    registry.record_attempt(first["id"], status="complete", job_count=7,
                            jd_pending_count=2, pagination_complete=True)
    registry.record_attempt(first["id"], status="failed", reason_code="timeout",
                            reason="listing timed out")
    record = registry.get(first["id"])

    assert record["status"] == "failed"
    assert record["job_count"] == 7
    assert record["last_success_job_count"] == 7
    assert record["pagination_complete"] is True
    assert [attempt["status"] for attempt in record["attempts"]] == ["failed", "complete"]
    assert record["attempts"][0]["job_count"] == 0


def test_manual_entry_preserves_original_and_list_filters(tmp_path):
    registry = _registry(tmp_path)
    row = registry.upsert_source(
        source="feed", source_record_id="a", company_name="Alpha",
        source_url="https://feed.example/a", entry_url="https://jobs.example/old",
    )
    updated = registry.set_entry_url(row["id"], "https://jobs.example/manual",
                                     __import__("datetime").datetime.fromisoformat(row["updated_at"]))
    assert updated["entry_url"] == "https://jobs.example/manual"
    assert updated["original_entry_url"] == "https://jobs.example/old"
    assert registry.list_sources(q="Alpha")["total"] == 1
    assert registry.list_sources(status="pending")["items"][0]["id"] == row["id"]


def test_unsafe_discovery_urls_are_saved_as_unusable_evidence(tmp_path):
    registry = _registry(tmp_path)
    row = registry.upsert_source(
        source="snapshot", source_record_id="unsafe", company_name="Unsafe",
        source_url="file:///private/source.json", entry_url="https://user:pass@example.test/jobs",
    )
    assert row["status"] == "unusable"
    assert row["source_url"] == "file:///private/source.json"
    assert row["entry_url"] == "https://[REDACTED]@example.test/jobs"
    assert row["reason_code"] == "unsafe_url"


def test_unsafe_reimport_does_not_overwrite_manual_entry_or_state(tmp_path):
    registry = _registry(tmp_path)
    row = registry.upsert_source(
        source="snapshot", source_record_id="manual", company_name="Manual",
        source_url="https://source.example/rows", entry_url="https://jobs.example/original",
    )
    manual = registry.set_entry_url(
        row["id"], "https://jobs.example/manual", __import__("datetime").datetime.fromisoformat(row["updated_at"])
    )
    registry.record_attempt(manual["id"], status="complete", job_count=3, pagination_complete=True)
    restored = registry.upsert_source(
        source="snapshot", source_record_id="manual", company_name="Manual",
        source_url="file:///private/source.json", entry_url="http://127.0.0.1/jobs",
    )
    assert restored["entry_url"] == "https://jobs.example/manual"
    assert restored["status"] == "complete"
    assert restored["job_count"] == 3


def test_running_entry_cannot_be_changed_and_detail_attempts_are_bounded(tmp_path):
    registry = _registry(tmp_path)
    row = registry.upsert_source(
        source="feed", source_record_id="running", company_name="Running",
        source_url="https://feed.example/rows", entry_url="https://jobs.example/old",
    )
    registry.record_attempt(row["id"], status="running", attempted_url=row["entry_url"])
    current = registry.get(row["id"])
    try:
        registry.set_entry_url(
            row["id"], "https://jobs.example/manual",
            __import__("datetime").datetime.fromisoformat(current["updated_at"]),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("running source entry was editable")

    for index in range(55):
        registry.record_attempt(row["id"], status="failed", reason=f"attempt-{index}")
    detail = registry.get(row["id"])
    assert len(detail["attempts"]) == 50
    assert detail["attempts_total"] == 56


def test_list_order_is_stable_when_updated_at_matches(tmp_path):
    registry = _registry(tmp_path)
    first = registry.upsert_source(
        source="feed", source_record_id="a", company_name="A",
        source_url="https://feed.example/a", entry_url="https://jobs.example/a",
    )
    second = registry.upsert_source(
        source="feed", source_record_id="b", company_name="B",
        source_url="https://feed.example/b", entry_url="https://jobs.example/b",
    )
    from datetime import datetime, timezone
    from packages.discovery.company_registry import CompanySourceRecord

    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with registry.storage.write_transaction() as session:
        session.get(CompanySourceRecord, first["id"]).updated_at = stamp
        session.get(CompanySourceRecord, second["id"]).updated_at = stamp
    items = registry.list_sources(page_size=2)["items"]
    assert [item["id"] for item in items] == sorted(
        [first["id"], second["id"]], reverse=True
    )


def test_unsafe_attempt_url_is_retained_redacted_without_raising(tmp_path):
    registry = _registry(tmp_path)
    row = registry.upsert_source(
        source="daily", source_record_id="unsafe-attempt", company_name="Unsafe Attempt",
        source_url="https://source.example/rows", entry_url="https://jobs.example/old",
    )
    detail = registry.record_attempt(
        row["id"], status="failed", attempted_url="file:///private/jobs.json",
        final_url="https://user:secret@example.test/jobs?access_token=secret&x=1",
        reason_code="unsafe_source", reason="source was not crawlable",
    )
    attempt = detail["attempts"][0]
    assert attempt["attempted_url"] == "file:///private/jobs.json"
    assert attempt["final_url"] == (
        "https://[REDACTED]@example.test/jobs?access_token=%5BREDACTED%5D&x=1"
    )
    assert "secret" not in str(detail)
