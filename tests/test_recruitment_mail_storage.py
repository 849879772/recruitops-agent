from __future__ import annotations

from datetime import date, datetime, timezone
import json

import pytest
from sqlalchemy import inspect, select

from packages.recruitment_mail import (
    CompanyCandidate,
    EmailMessage,
    JobCandidate,
    MailIdentity,
    ParsedRecruitmentEmail,
    RecruitmentMailProcessingStatus,
    RecruitmentMailRecord,
    RecruitmentMailStore,
    RecruitmentMessageCategory,
)
from packages.storage import Storage, create_storage_engine


UTC = timezone.utc


def _store(tmp_path) -> RecruitmentMailStore:
    engine = create_storage_engine(f"sqlite:///{tmp_path / 'recruitment-mail.db'}")
    return RecruitmentMailStore(Storage(engine))


def _message(*, message_id: str = "provider-message-1", received_at: datetime | None = None) -> EmailMessage:
    return EmailMessage(
        identity=MailIdentity(
            message_id=message_id,
            mailbox="INBOX",
            thread_id="thread-1",
            account_ref="account-local",
        ),
        sender="招聘团队 <recruiter@example.com>",
        recipients=["candidate@example.com"],
        subject="【星河科技】面试邀请",
        body_text=(
            "公司：星河科技有限公司\n职位：后端开发工程师\n"
            "面试时间：2026年8月20日 下午2点半\n"
            "联系电话：13812345678\n"
            "api_key=sk-local-secret-value cookie=session-secret "
            "身份证号：110101199001011234"
        ),
        received_at=received_at or datetime(2026, 8, 20, 6, 0, tzinfo=UTC),
    )


def _parsed_message(
    *,
    message_id: str = "provider-message-1",
    received_at: datetime | None = None,
    subject: str = "【星河科技】面试邀请",
    body_text: str | None = None,
    category: RecruitmentMessageCategory = RecruitmentMessageCategory.INTERVIEW,
) -> ParsedRecruitmentEmail:
    return ParsedRecruitmentEmail(
        identity=MailIdentity(
            message_id=message_id,
            mailbox="INBOX",
            thread_id="thread-1",
            account_ref="account-local",
        ),
        sender="招聘团队 <recruiter@example.com>",
        recipients=["candidate@example.com"],
        subject=subject,
        body_text=body_text or (
            "公司：星河科技有限公司\n职位：后端开发工程师\n"
            "面试时间：2026年8月20日 下午2点半\n"
            "联系电话：13812345678\n"
            "api_key=sk-local-secret-value cookie=session-secret "
            "身份证号：110101199001011234"
        ),
        received_at=received_at or datetime(2026, 8, 20, 6, 0, tzinfo=UTC),
        category=category,
        company_candidates=[
            CompanyCandidate(value="星河科技有限公司", evidence="正文", confidence=1.0)
        ],
        job_candidates=[
            JobCandidate(value="后端开发工程师", evidence="正文", confidence=1.0)
        ],
        confidence=1.0,
    )


def test_store_registers_bounded_local_schema_and_redacts_before_persisting(tmp_path) -> None:
    store = _store(tmp_path)
    record = store.upsert(_message(), parsed=_parsed_message())

    columns = {
        column["name"]: column["type"]
        for column in inspect(store.storage.engine).get_columns("recruitment_emails")
    }
    assert {"raw_metadata", "parsed_result", "content_digest", "processing_status"} <= set(columns)
    assert columns["body_text"].length == 200_000

    serialized = json.dumps(record.raw_metadata, ensure_ascii=False) + json.dumps(
        record.parsed_result, ensure_ascii=False
    ) + record.body_text
    assert "13812345678" not in serialized
    assert "110101199001011234" not in serialized
    assert "sk-local-secret-value" not in serialized
    assert "session-secret" not in serialized
    assert "recruiter@example.com" not in serialized
    assert "candidate@example.com" not in serialized
    assert "html_body" not in record.raw_metadata
    assert record.parsed_result["category"] == "interview"
    assert record.pending_confirmation_reasons == []


def test_upsert_is_idempotent_for_message_identity_and_updates_parsed_content(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.upsert(_message(), parsed=_parsed_message())

    changed = _message()
    changed.subject = "【星河科技】录用通知"
    changed.body_text = "公司：星河科技有限公司\n职位：后端开发工程师\n恭喜您获得录用。"
    second = store.upsert(
        changed,
        parsed=_parsed_message(
            subject="【星河科技】录用通知",
            body_text="公司：星河科技有限公司\n职位：后端开发工程师\n恭喜您获得录用。",
            category=RecruitmentMessageCategory.OFFER,
        ),
        processing_status=RecruitmentMailProcessingStatus.PROCESSED,
    )

    assert second.id == first.id
    assert second.processing_status == "processed"
    assert second.category == "offer"
    with store.storage.session() as session:
        assert session.scalar(select(RecruitmentMailRecord).where(RecruitmentMailRecord.id == first.id))
        assert session.query(RecruitmentMailRecord).count() == 1


def test_content_digest_is_fallback_key_and_queries_support_date_category_and_status(tmp_path) -> None:
    store = _store(tmp_path)
    message = _message(message_id="local-message")
    parsed = _parsed_message(message_id="local-message")
    first = store.upsert(message, parsed=parsed)
    second = store.upsert(message.model_copy(deep=True), parsed=parsed.model_copy(deep=True))
    assert first.id == second.id

    store.update_associations(
        first.id,
        application_id="application-1",
        job_id="job-1",
        company_id="company-1",
    )
    store.update_processing_status(first.id, "needs_confirmation")

    results = store.query(
        start_date=date(2026, 8, 20),
        end_date=date(2026, 8, 20),
        category="interview",
        status="needs_confirmation",
    )
    assert [item.id for item in results] == [first.id]
    linked = store.get(first.id)
    assert linked is not None
    assert (linked.application_id, linked.job_id, linked.company_id) == (
        "application-1",
        "job-1",
        "company-1",
    )
    assert store.get_by_content_digest(first.content_digest).id == first.id


def test_association_updates_preserve_other_links_and_reject_unbounded_json(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.upsert(_message(), parsed=_parsed_message())
    store.update_associations(first.id, application_id="application-1", job_id="job-1")
    updated = store.update_associations(first.id, company_id="company-1")
    assert (updated.application_id, updated.job_id, updated.company_id) == (
        "application-1",
        "job-1",
        "company-1",
    )

    oversized = _message()
    oversized.body_text = "x" * 200_001
    with pytest.raises(ValueError, match="body_text"):
        store.upsert(oversized)


def test_store_does_not_need_or_attempt_network_access(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()))
    record = store.upsert(_message())
    assert record.processing_status == "pending"
