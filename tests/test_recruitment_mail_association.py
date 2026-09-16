from datetime import datetime, timezone

from packages.domain.models import Application, ApplicationStage, ScheduleEvent
from packages.recruitment_mail import (
    CompanyCandidate,
    JobCandidate,
    MailIdentity,
    LocationCandidate,
    ParsedRecruitmentEmail,
    RecruitmentMessageCategory,
    TimeCandidate,
    associate_recruitment_email,
)


def _application(identifier: str, company: str, title: str) -> Application:
    return Application(
        id=identifier,
        company_name=company,
        job_title=title,
        stage=ApplicationStage.APPLIED,
        idempotency_key=f"application:{identifier}",
        source="fixture",
        source_ref=identifier,
    )


def test_unique_mail_match_creates_stage_and_schedule_drafts() -> None:
    parsed = ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mail-1"),
        sender="字节跳动招聘 <hr@example.com>",
        subject="机器人软件工程师面试邀请",
        body_text="字节跳动 机器人软件工程师 面试时间 2026-08-21 10:30 https://example.com/interview",
        received_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        category=RecruitmentMessageCategory.INTERVIEW,
        company_candidates=[
            CompanyCandidate(value="字节跳动", evidence="邮件正文", confidence=1.0)
        ],
        job_candidates=[
            JobCandidate(value="机器人软件工程师", evidence="邮件主题", confidence=1.0)
        ],
        time_candidates=[
            TimeCandidate(
                value="2026-08-21 10:30",
                evidence="邮件正文",
                confidence=1.0,
                normalized=datetime(2026, 8, 21, 10, 30, tzinfo=timezone.utc),
            )
        ],
        confidence=0.0,
    )
    applications = [
        _application("1", "字节跳动", "机器人软件工程师"),
        _application("2", "另一家公司", "C++工程师"),
    ]

    result = associate_recruitment_email(parsed, applications)

    assert result.status in {"matched", "review_required"}
    assert result.match.application_id == "1"
    assert result.stage_draft.target_stage is ApplicationStage.INTERVIEW1
    assert result.schedule_drafts[0].event_type == "面试"
    assert result.evidence[0].source == "recruitment_mail"
    assert result.evidence[0].source_ref == "INBOX/mail-1"
    assert result.stage_draft.evidence_refs == result.evidence
    assert result.schedule_drafts[0].evidence_refs == result.evidence


def test_same_company_same_title_stops_as_ambiguous() -> None:
    parsed = ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mail-2"),
        subject="测试工程师笔试通知",
        body_text="示例公司 测试工程师 笔试通知",
        category=RecruitmentMessageCategory.WRITTEN_TEST,
        company_candidates=[
            CompanyCandidate(value="示例公司", evidence="邮件正文", confidence=1.0)
        ],
        job_candidates=[
            JobCandidate(value="测试工程师", evidence="邮件主题", confidence=1.0)
        ],
        confidence=0.0,
    )
    applications = [
        _application("1", "示例公司", "测试工程师"),
        _application("2", "示例公司", "测试工程师"),
    ]

    result = associate_recruitment_email(parsed, applications)

    assert result.status == "ambiguous"
    assert result.match is None
    assert result.review_reasons == ["multiple_application_matches"]


def test_unrelated_mail_does_not_create_drafts() -> None:
    parsed = ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mail-3"),
        subject="每周资讯",
        body_text="这是一封普通新闻邮件。",
        category=RecruitmentMessageCategory.OTHER,
        confidence=0.0,
    )

    result = associate_recruitment_email(
        parsed,
        [_application("1", "示例公司", "C++工程师")],
    )

    assert result.status == "unresolved"
    assert result.stage_draft is None
    assert result.schedule_drafts == []


def test_interview_without_end_time_does_not_invent_one_hour_conflict() -> None:
    parsed = ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mail-4"),
        subject="【星河科技】机器人软件工程师面试安排",
        body_text=(
            "公司：星河科技有限公司\n"
            "职位：机器人软件工程师\n"
            "面试时间：2026年8月24日 14:00\n"
            "面试地点：上海市浦东新区张江路1号\n"
            "请于 2026年8月22日 18:00 前确认。\n"
            "https://jobs.example.test/meeting?id=1"
        ),
        category=RecruitmentMessageCategory.INTERVIEW,
        company_candidates=[
            CompanyCandidate(value="星河科技有限公司", evidence="正文", confidence=1.0)
        ],
        job_candidates=[
            JobCandidate(value="机器人软件工程师", evidence="正文", confidence=1.0)
        ],
        location_candidates=[
            LocationCandidate(
                value="上海市浦东新区张江路1号",
                evidence="面试地点：上海市浦东新区张江路1号",
                confidence=1.0,
            )
        ],
        time_candidates=[
            TimeCandidate(
                value="2026年8月24日 14:00",
                evidence="面试时间：2026年8月24日 14:00",
                confidence=1.0,
                normalized=datetime(2026, 8, 24, 14, tzinfo=timezone.utc),
            )
        ],
        confidence=0.0,
    )
    existing = ScheduleEvent(
        id="existing-event-1",
        title="笔试 · 另一家公司",
        event_date=datetime(2026, 8, 24, tzinfo=timezone.utc).date(),
        event_time=datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc).time(),
        event_type="笔试",
        company_name="另一家公司",
        job_title="测试工程师",
        application_stage=ApplicationStage.WRITTEN,
        starts_at=datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc),
        source="fixture",
        source_ref="schedule/existing-event-1",
    )

    result = associate_recruitment_email(
        parsed,
        [_application("1", "星河科技", "机器人软件工程师")],
        schedule_events=[existing],
    )

    assert result.match is not None and result.match.application_id == "1"
    assert result.status == "matched"
    assert result.requires_confirmation is False
    assert result.schedule_conflicts == []
    assert "schedule_conflict" not in result.review_reasons
    assert len(result.schedule_drafts) == 1
    assert result.schedule_drafts[0].location_or_link == "上海市浦东新区张江路1号"


def test_neutral_mail_confidence_does_not_block_explicit_identity_match() -> None:
    parsed = ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mail-5"),
        subject="机器人软件工程师面试邀请",
        body_text="公司：示例公司\n职位：机器人软件工程师\n面试时间：2026-08-21 10:30",
        category=RecruitmentMessageCategory.INTERVIEW,
        company_candidates=[
            CompanyCandidate(value="示例公司", evidence="公司字段", confidence=1.0)
        ],
        job_candidates=[
            JobCandidate(value="机器人软件工程师", evidence="职位字段", confidence=1.0)
        ],
        time_candidates=[
            TimeCandidate(
                value="2026-08-21 10:30",
                evidence="面试时间：2026-08-21 10:30",
                confidence=1.0,
                normalized=datetime(2026, 8, 21, 10, 30, tzinfo=timezone.utc),
            )
        ],
        confidence=0.0,
        pending_confirmation_reasons=[],
    )

    result = associate_recruitment_email(
        parsed,
        [_application("1", "示例公司", "机器人软件工程师")],
    )

    assert result.status == "matched"
    assert result.requires_confirmation is False
    assert result.stage_draft is not None
    assert "mail_confidence_too_low" not in result.review_reasons


def test_mail_evidence_confirmation_uses_current_review_reason() -> None:
    parsed = ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mail-6"),
        subject="机器人软件工程师面试邀请",
        body_text="公司：示例公司\n职位：机器人软件工程师",
        category=RecruitmentMessageCategory.INTERVIEW,
        company_candidates=[
            CompanyCandidate(value="示例公司", evidence="公司字段", confidence=1.0)
        ],
        job_candidates=[
            JobCandidate(value="机器人软件工程师", evidence="职位字段", confidence=1.0)
        ],
        confidence=0.0,
        requires_confirmation=True,
    )

    result = associate_recruitment_email(
        parsed,
        [_application("1", "示例公司", "机器人软件工程师")],
    )

    assert result.status == "review_required"
    assert result.requires_confirmation is True
    assert "mail_evidence_requires_confirmation" in result.review_reasons
    assert "mail_parser_requires_confirmation" not in result.review_reasons


def test_old_company_only_confirmation_resolves_for_noop_settlement() -> None:
    parsed = ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mail-iflytek-old"),
        subject="感谢您投递科大讯飞校园招聘职位",
        body_text="感谢您关注科大讯飞校园招聘职位。",
        received_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        category=RecruitmentMessageCategory.APPLICATION_CONFIRMATION,
        company_candidates=[
            CompanyCandidate(value="科大讯飞股份有限公司", evidence="发件人", confidence=1.0)
        ],
        job_candidates=[],
        confidence=0.0,
    )
    application = _application("1", "科大讯飞", "机器人软件工程师(J13410)").model_copy(
        update={
            "stage": ApplicationStage.REJECTED,
            "source_status_synced_at": datetime(2026, 8, 21, tzinfo=timezone.utc),
        }
    )

    result = associate_recruitment_email(parsed, [application])

    assert result.match is not None and result.match.application_id == "1"
    assert result.match.reasons == ["stale_company_only_noop"]
    assert result.stage_draft is None
