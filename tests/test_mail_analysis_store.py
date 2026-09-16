from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.recruitment_mail import EmailMessage, MailIdentity, RecruitmentMailStore
from packages.recruitment_mail.analysis_store import (
    get_model_analysis,
    save_model_analysis,
)
from packages.storage import Storage


def _store(tmp_path: Path) -> RecruitmentMailStore:
    return RecruitmentMailStore(Storage.from_url(f"sqlite:///{tmp_path / 'mail.db'}"))


def _record(store: RecruitmentMailStore):
    return store.upsert(
        EmailMessage(
            identity=MailIdentity(
                message_id="mail-analysis-1",
                mailbox="INBOX",
                account_ref="account-a",
            ),
            sender="jobs@example.com",
            recipients=["candidate@example.com"],
            subject="面试邀请",
            body_text="请参加后端开发工程师面试。",
            source_metadata={
                "imap_uid": "17",
                "uid_validity": "uid-1",
                "authentication_results": [
                    {"method": "dkim", "result": "pass", "aligned": True}
                ],
            },
        )
    )


def test_save_preserves_transport_auth_and_does_not_process_mail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record(store)
    transport = record.raw_metadata["transport"]

    saved = save_model_analysis(
        store,
        record.id,
        record.content_digest,
        "mail-analysis.v1",
        {"event": "interview", "candidate_application_id": None},
        "proposed",
        model="gpt-5.6-luna",
    )

    updated = store.get(record.id)
    assert updated is not None
    assert updated.raw_metadata["transport"] == transport
    assert updated.raw_metadata["model_analysis"] == saved
    assert updated.processing_status == "pending"
    assert updated.application_id is None
    assert saved["digest"] == record.content_digest
    assert saved["version"] == "mail-analysis.v1"
    assert saved["model"] == "gpt-5.6-luna"
    assert "DeepSeek" not in json.dumps(saved, ensure_ascii=False)
    assert saved["created_at"]


def test_stale_digest_is_rejected_without_writing_analysis(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record(store)

    with pytest.raises(ValueError, match="stale content_digest"):
        save_model_analysis(
            store,
            record.id,
            "b" * 64,
            "mail-analysis.v1",
            {"event": "interview"},
            "proposed",
        )

    assert get_model_analysis(store, record.id) is None


def test_exact_replay_is_idempotent_and_keeps_first_created_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record(store)
    payload = {"event": "interview", "confidence": 0.91}

    first = save_model_analysis(
        store,
        record.id,
        record.content_digest,
        "mail-analysis.v1",
        payload,
        "proposed",
        model="gpt-5.6-luna",
    )
    replay = save_model_analysis(
        store,
        record.id,
        record.content_digest,
        "mail-analysis.v1",
        payload,
        "proposed",
        model="gpt-5.6-luna",
    )

    assert replay == first
    assert get_model_analysis(store, record.id) == first


def test_missing_record_is_rejected_and_get_is_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(KeyError, match="mail record"):
        save_model_analysis(
            store,
            "missing-record",
            "a" * 64,
            "mail-analysis.v1",
            {"event": "interview"},
            "proposed",
        )

    assert get_model_analysis(store, "missing-record") is None


def test_payload_over_64kb_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _record(store)

    with pytest.raises(ValueError, match="64KB"):
        save_model_analysis(
            store,
            record.id,
            record.content_digest,
            "mail-analysis.v1",
            {"text": "x" * 64_001},
            "proposed",
        )
