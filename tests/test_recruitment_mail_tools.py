from datetime import datetime, timezone

from packages.domain.models import Application, ApplicationStage
from packages.recruitment_mail import (
    CompanyCandidate,
    JobCandidate,
    LinkCandidate,
    MailIdentity,
    LocationCandidate,
    ParsedRecruitmentEmail,
    RecruitmentMailStore,
    RecruitmentMessageCategory,
    TimeCandidate,
)
from packages.recruitment_mail.analysis_store import save_model_analysis
from packages.recruitment_mail.model_analysis import MAIL_ANALYSIS_VERSION
from packages.storage import Storage
from packages.tools import (
    RecruitmentMailDetailInput,
    RecruitmentMailReviewInput,
    RecruitmentMailSearchInput,
    ToolErrorCode,
    ToolStatus,
    get_recruitment_mail,
    review_recruitment_mail,
    search_recruitment_mail,
)
from tests.test_typed_tools import InMemoryRepository


def _store() -> RecruitmentMailStore:
    return RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))


def _parsed() -> ParsedRecruitmentEmail:
    starts_at = datetime(2026, 8, 21, 10, 30, tzinfo=timezone.utc)
    return ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mail-tool-1"),
        sender="hr@example.com",
        subject="示例公司 C++开发工程师面试邀请",
        body_text="请于 8 月 21 日参加面试",
        received_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        category=RecruitmentMessageCategory.INTERVIEW,
        company_candidates=[
            CompanyCandidate(value="示例公司", evidence="主题", confidence=1.0)
        ],
        job_candidates=[
            JobCandidate(value="C++开发工程师", evidence="主题", confidence=1.0)
        ],
        location_candidates=[
            LocationCandidate(value="线上会议", evidence="邮件正文", confidence=1.0)
        ],
        link_candidates=[
            LinkCandidate(url="https://example.test/interview", evidence="邮件正文", confidence=1.0)
        ],
        time_candidates=[
            TimeCandidate(
                value="2026-08-21 10:30",
                evidence="邮件正文",
                confidence=1.0,
                normalized=starts_at,
            )
        ],
        confidence=0.95,
    )


def _save_review_analysis(
    store: RecruitmentMailStore,
    record,
    *,
    company_name: str = "示例公司",
    job_title: str = "C++开发工程师",
    event_type: str = "interview",
    candidate_application_id: str = "1",
) -> None:
    save_model_analysis(
        store,
        record.id,
        record.content_digest,
        MAIL_ANALYSIS_VERSION,
        {
            "record_id": record.id,
            "content_digest": record.content_digest,
            "company_name": company_name,
            "job_title": job_title,
            "job_code": None,
            "event_type": event_type,
            "event_time": None,
            "deadline": None,
            "evidence_quotes": [record.subject],
            "candidate_application_id": candidate_application_id,
            "match_reason": "explicit source-bound fixture",
            "action_summary": None,
        },
        "proposed",
        model="fixture-model",
    )


def test_mail_search_and_detail_return_redacted_persisted_models() -> None:
    store = _store()
    record = store.upsert(_parsed())

    searched = search_recruitment_mail(RecruitmentMailSearchInput(), store)
    detailed = get_recruitment_mail(RecruitmentMailDetailInput(record_id=record.id), store)

    assert searched.status is ToolStatus.SUCCESS
    assert searched.data.total == 1
    assert searched.data.items[0].id == record.id
    assert detailed.status is ToolStatus.SUCCESS
    assert detailed.data.message.category is RecruitmentMessageCategory.INTERVIEW


def test_mail_search_total_ignores_pagination_and_categories_exclude_other() -> None:
    store = _store()
    store.upsert(_parsed())
    store.upsert(
        _parsed().model_copy(
            update={
                "identity": MailIdentity(message_id="mail-tool-2"),
                "subject": "另一家公司 面试邀请",
            }
        )
    )
    store.upsert(
        _parsed().model_copy(
            update={
                "identity": MailIdentity(message_id="mail-tool-other"),
                "subject": "账号安全提醒",
                "category": RecruitmentMessageCategory.OTHER,
            }
        )
    )

    result = search_recruitment_mail(
        RecruitmentMailSearchInput(
            categories=[RecruitmentMessageCategory.INTERVIEW],
            limit=1,
        ),
        store,
    )

    assert result.data is not None
    assert result.data.total == 2
    assert len(result.data.items) == 1
    assert result.data.items[0].company_name == "示例公司"


def test_mail_review_creates_approval_previews_without_writing() -> None:
    store = _store()
    record = store.upsert(_parsed())
    _save_review_analysis(store, record)
    repository = InMemoryRepository()
    repository.applications = [
        Application(
            id="1",
            company_name="示例公司",
            job_title="C++开发工程师",
            job_id="job-1",
            stage=ApplicationStage.APPLIED,
            idempotency_key="application:1",
            source="fixture",
            source_ref="1",
        )
    ]

    result = review_recruitment_mail(
        RecruitmentMailReviewInput(record_id=record.id), store, repository
    )

    assert result.status is ToolStatus.SUCCESS
    assert result.data.association.status == "matched"
    operations = {item.operation.value for item in result.data.approval_previews}
    assert operations == {"application_stage_update"}
    assert result.data.association.schedule_drafts == []
    stage_preview = result.data.approval_previews[0]
    assert stage_preview.payload["application_id"] == 1
    assert stage_preview.evidence[0].source == "recruitment_mail"
    assert stage_preview.evidence[0].source_ref == record.id
    assert stage_preview.payload["source"] == "recruitment_mail"
    assert stage_preview.payload["source_ref"] == record.id
    assert stage_preview.payload["audit_evidence"][0]["source_ref"] == record.id
    assert repository.applications[0].stage is ApplicationStage.APPLIED


def test_fixed_163_mail_review_returns_human_approval_previews_without_writing() -> None:
    store = _store()
    record = store.upsert(
        ParsedRecruitmentEmail(
            identity=MailIdentity(message_id="mail-tool-163"),
            sender="示例公司招聘 <campus@163.com>",
            subject="【示例公司】C++开发工程师面试邀请",
            body_text=(
                "公司：示例公司\n"
                "职位：C++开发工程师\n"
                "面试时间：2026年8月21日 10:30\n"
                "面试地点：线上\n"
                "请于 2026年8月20日 18:00 前确认。\n"
                "入口：https://example.test/interview"
            ),
            category=RecruitmentMessageCategory.INTERVIEW,
            company_candidates=[
                CompanyCandidate(value="示例公司", evidence="正文", confidence=1.0)
            ],
            job_candidates=[
                JobCandidate(value="C++开发工程师", evidence="正文", confidence=1.0)
            ],
            location_candidates=[
                LocationCandidate(value="线上", evidence="正文", confidence=1.0)
            ],
            link_candidates=[
                LinkCandidate(url="https://example.test/interview", evidence="正文", confidence=1.0)
            ],
            time_candidates=[
                TimeCandidate(
                    value="2026-08-21 10:30",
                    evidence="面试时间：2026年8月21日 10:30",
                    confidence=1.0,
                    normalized=datetime(2026, 8, 21, 10, 30, tzinfo=timezone.utc),
                )
            ],
            confidence=0.95,
        )
    )
    _save_review_analysis(store, record)
    repository = InMemoryRepository()
    original_stage = repository.applications[0].stage

    result = review_recruitment_mail(
        RecruitmentMailReviewInput(record_id=record.id), store, repository
    )

    assert result.data is not None
    assert result.data.association.status == "matched"
    assert result.data.association.requires_confirmation is False
    assert {item.operation.value for item in result.data.approval_previews} == {
        "application_stage_update",
    }
    assert result.data.association.schedule_drafts == []
    assert repository.applications[0].stage is original_stage


def test_mail_review_without_persisted_model_analysis_returns_invalid_source() -> None:
    store = _store()
    record = store.upsert(_parsed())

    result = review_recruitment_mail(
        RecruitmentMailReviewInput(record_id=record.id),
        store,
        InMemoryRepository(),
    )

    assert result.status is ToolStatus.FAILURE
    assert result.success is False
    assert result.error_code is ToolErrorCode.INVALID_SOURCE
