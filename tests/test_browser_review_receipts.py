from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from packages.storage import (
    BrowserReviewReceipt,
    BrowserReviewReceiptStore,
    Storage,
    create_storage_engine,
    initialize_schema,
)


CAPTURED_AT = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _storage(database_url: str = "sqlite:///:memory:") -> Storage:
    engine = create_storage_engine(database_url)
    initialize_schema(engine)
    return Storage(engine)


def _receipt(**overrides: object) -> BrowserReviewReceipt:
    values: dict[str, object] = {
        "review_id": "review-1",
        "target_id": "target-1",
        "action_attempt_id": "attempt-1",
        "normalized_url": "https://ats.example/applications",
        "application_ids": ["app-1", "app-2"],
        "status": "authorized",
    }
    values.update(overrides)
    return BrowserReviewReceipt(**values)


def _as_utc(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo is not None
        else value.replace(tzinfo=timezone.utc)
    )


def test_receipt_table_has_named_composite_unique_constraint() -> None:
    storage = _storage()

    constraints = inspect(storage.engine).get_unique_constraints("browser_review_receipts")

    assert {
        "review_id",
        "target_id",
        "action_attempt_id",
    } in [set(item["column_names"]) for item in constraints]


def test_sqlite_enforces_receipt_unique_key() -> None:
    storage = _storage()
    with storage.write_transaction() as session:
        session.add(_receipt())

    with pytest.raises(IntegrityError):
        with storage.write_transaction() as session:
            session.add(_receipt())
            session.flush()


def test_authorization_and_observation_are_idempotent() -> None:
    storage = _storage()
    store = BrowserReviewReceiptStore(storage)

    authorized = store.record_authorized_attempt(
        "review-1",
        "target-1",
        "attempt-1",
        "https://ats.example/applications",
        ["app-1", "app-2"],
    )
    authorized_replay = store.record_authorized_attempt(
        "review-1",
        "target-1",
        "attempt-1",
        "https://ats.example/applications",
        ["app-1", "app-2"],
    )
    assert authorized_replay.review_id == authorized.review_id
    assert _as_utc(authorized_replay.created_at) == _as_utc(authorized.created_at)
    assert authorized_replay.status == "authorized"

    entries = [{"application_id": "app-1", "status": "interview"}]
    observed = store.save_observation(
        "review-1",
        "target-1",
        "attempt-1",
        "https://ats.example/applications",
        ["app-1", "app-2"],
        entries,
        CAPTURED_AT,
        "observation-1",
    )
    replay = store.save_observation(
        "review-1",
        "target-1",
        "attempt-1",
        "https://ats.example/applications",
        ["app-1", "app-2"],
        entries,
        CAPTURED_AT,
        "observation-1",
    )

    assert replay.review_id == observed.review_id
    assert _as_utc(replay.created_at) == _as_utc(observed.created_at)
    assert replay.status == "observed"
    assert replay.observation_id == "observation-1"
    assert replay.entries == entries
    assert replay.captured_at is not None
    assert _as_utc(replay.captured_at) == CAPTURED_AT


def test_observation_requires_matching_authorization_and_conflicts_fail() -> None:
    storage = _storage()
    store = BrowserReviewReceiptStore(storage)

    with pytest.raises(ValueError, match="no authorized attempt"):
        store.save_observation(
            "review-1",
            "target-1",
            "attempt-1",
            "https://ats.example/applications",
            ["app-1"],
            [],
            CAPTURED_AT,
            "observation-1",
        )

    store.record_authorized_attempt(
        "review-1",
        "target-1",
        "attempt-1",
        "https://ats.example/applications",
        ["app-1"],
    )
    with pytest.raises(ValueError, match="does not match authorization"):
        store.save_observation(
            "review-1",
            "target-1",
            "attempt-1",
            "https://ats.example/other",
            ["app-1"],
            [],
            CAPTURED_AT,
            "observation-1",
        )

    store.save_observation(
        "review-1",
        "target-1",
        "attempt-1",
        "https://ats.example/applications",
        ["app-1"],
        [{"status": "applied"}],
        CAPTURED_AT,
        "observation-1",
    )
    with pytest.raises(ValueError, match="conflicts"):
        store.save_observation(
            "review-1",
            "target-1",
            "attempt-1",
            "https://ats.example/applications",
            ["app-1"],
            [{"status": "rejected"}],
            CAPTURED_AT,
            "observation-1",
        )


def test_completed_result_is_idempotent_and_conflicts_fail() -> None:
    store = BrowserReviewReceiptStore(_storage())
    store.record_authorized_attempt(
        "review-1",
        "target-1",
        "attempt-1",
        "https://ats.example/applications",
        ["app-1"],
    )
    store.save_observation(
        "review-1",
        "target-1",
        "attempt-1",
        "https://ats.example/applications",
        ["app-1"],
        [{"status": "written"}],
        CAPTURED_AT,
        "observation-1",
    )
    result = {"review": {"status": "succeeded"}, "write_approvals": []}

    completed = store.save_result(
        "review-1", "target-1", "attempt-1", result
    )
    replayed = store.save_result(
        "review-1", "target-1", "attempt-1", result
    )

    assert completed.status == "completed"
    assert replayed.result == result
    with pytest.raises(ValueError, match="conflicts"):
        store.save_result(
            "review-1",
            "target-1",
            "attempt-1",
            {"review": {"status": "failed"}},
        )

def test_save_or_get_is_idempotent_and_rejects_content_conflicts() -> None:
    storage = _storage()
    store = BrowserReviewReceiptStore(storage)
    first = store.save_or_get(_receipt())
    replay = store.save_or_get(
        review_id="review-1",
        target_id="target-1",
        action_attempt_id="attempt-1",
        normalized_url="https://ats.example/applications",
        application_ids=["app-1", "app-2"],
        status="authorized",
    )

    assert replay.review_id == first.review_id
    assert _as_utc(replay.created_at) == _as_utc(first.created_at)
    with pytest.raises(ValueError, match="conflicts"):
        store.save_or_get(
            review_id="review-1",
            target_id="target-1",
            action_attempt_id="attempt-1",
            normalized_url="https://ats.example/applications",
            application_ids=["app-2", "app-1"],
            status="authorized",
        )


def test_new_store_instance_reads_persisted_observation(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'browser-reviews.db'}"
    first_storage = _storage(database_url)
    BrowserReviewReceiptStore(first_storage).record_authorized_attempt(
        "review-1",
        "target-1",
        "attempt-1",
        "https://ats.example/applications",
        ["app-1"],
    )
    BrowserReviewReceiptStore(first_storage).save_observation(
        "review-1",
        "target-1",
        "attempt-1",
        "https://ats.example/applications",
        ["app-1"],
        [{"status": "interview"}],
        CAPTURED_AT,
        "observation-1",
    )
    first_storage.engine.dispose()

    second_storage = Storage.from_url(database_url)
    saved = BrowserReviewReceiptStore(second_storage).get(
        "review-1", "target-1", "attempt-1"
    )

    assert saved is not None
    assert saved.status == "observed"
    assert saved.normalized_url == "https://ats.example/applications"
    assert saved.application_ids == ["app-1"]
    assert saved.observation_id == "observation-1"
    assert saved.entries == [{"status": "interview"}]
