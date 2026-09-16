"""Use real persisted mail in an in-memory database; never change production rows."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.config import Settings
from packages.storage import Storage, ApplicationSnapshot
from packages.recruitment_mail import RecruitmentMailStore
from packages.recruitment_mail.storage import RecruitmentMailRecord
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.recruitment_mail.processing import process_pending_mail


def main():
    settings = Settings()
    if "--samples" in sys.argv:
        return samples(settings)
    from sqlalchemy.engine import make_url
    url = make_url(settings.database_url).update_query_dict({"connect_timeout": "5"})
    source = Storage.from_url(url.render_as_string(hide_password=False))
    source_store = RecruitmentMailStore(source)
    fixture = Storage.from_url("sqlite+pysqlite:///:memory:")
    store = RecruitmentMailStore(fixture)
    store.query(limit=1)
    selected = {"mail-b6931382c409c48844ecdd65f328d825",
                "mail-f8e12cd12f87d76131b3277500b9ab71",
                "mail-8d8a8db67964f564a1e7be9340198d0a"}
    records = source_store.query(limit=200)
    security = next((r for r in records if "安全" in r.subject or "验证" in r.subject), None)
    if security:
        selected.add(security.id)
    with source.session() as read, fixture.write_transaction() as write:
        for app in read.query(ApplicationSnapshot).all():
            values = {c.name: getattr(app, c.name) for c in ApplicationSnapshot.__table__.columns}
            if app.id in {"4", "8"}:
                values.update(stage="applied", source_stage="applied", source_status="applied",
                              source_status_synced_at=None, stage_history=[])
            write.add(ApplicationSnapshot(**values))
        for record in records:
            if record.id not in selected:
                continue
            values = {c.name: getattr(record, c.name) for c in RecruitmentMailRecord.__table__.columns}
            metadata = dict(values["raw_metadata"])
            metadata.pop("model_analysis", None)
            metadata.pop("model_processing", None)
            values.update(raw_metadata=metadata, processing_status="pending", processing_error=None,
                          application_id=None, job_id=None, company_id=None, processed_at=None)
            write.add(RecruitmentMailRecord(**values))
    repo = PostgresRecruitmentRepository(fixture)
    result = process_pending_mail(store, repo, settings, record_ids=list(selected))
    replay = process_pending_mail(store, repo, settings, record_ids=list(selected))
    print(json.dumps({"isolated": True, "result": result, "replay": replay,
                      "applications": [{"id": a.id, "stage": a.stage.value}
                                       for a in repo.list_applications() if a.id in {"1", "4", "8"}]},
                     ensure_ascii=True))


def samples(settings):
    """Model checks on transcribed examples; authentication here is a test fixture."""
    from datetime import datetime, timezone
    from packages.recruitment_mail import EmailMessage, MailIdentity
    from packages.recruitment_mail.preparation import prepare_mail_for_model
    fixture = Storage.from_url("sqlite+pysqlite:///:memory:")
    store = RecruitmentMailStore(fixture)
    store.query(limit=1)
    cases = [
        ("4", "DJI 大疆", "AI DevOps 工程师（深圳）", "来自DJI 大疆的测评邀请",
         "感谢参加 DJI 大疆 2027 校园招聘并投递AI DevOps 工程师（深圳），现诚邀您参与我们的在线测评！", "applied"),
        ("8", "浙江大华技术股份有限公司", "【研发中心】2027届具身智能算法工程师(J24413)", "大华股份辞谢信",
         "感谢您对大华股份的关注。很遗憾，您没有通过【研发中心】2027届具身智能算法工程师的简历筛选。", "applied"),
        ("1", "科大讯飞", "机器人软件工程师(J13410)", "感谢您投递科大讯飞校园招聘职位",
         "非常感谢您关注科大讯飞校园招聘职位！您可以登录科大讯飞校园招聘官网查看岗位应聘流程。", "rejected"),
    ]
    for ident, company, title, subject, body, stage in cases:
        message = EmailMessage(identity=MailIdentity(message_id="sample-" + ident),
            subject=subject, sender=company, body_text=body,
            received_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
            source_metadata={"authentication_results": [{"method": "dkim", "result": "pass",
                              "authserv_id": "fixture", "aligned": True}]})
        store.upsert(message, parsed=prepare_mail_for_model(message), source="imap_readonly")
        with fixture.write_transaction() as session:
            session.add(ApplicationSnapshot(id=ident, company_name=company, job_title=title, stage=stage,
                source_stage=stage, source_status=stage,
                source_status_synced_at=datetime(2026, 8, 21, tzinfo=timezone.utc) if stage == "rejected" else None,
                stage_history=[], source="fixture", source_ref=ident, idempotency_key="app-" + ident))
    security = EmailMessage(identity=MailIdentity(message_id="sample-security"),
                            subject="网易邮箱帐号安全通知", sender="网易邮箱帐号安全",
                            body_text="新设备登录提醒，请核对是否本人操作。")
    store.upsert(security, parsed=prepare_mail_for_model(security), source="imap_readonly")
    repo = PostgresRecruitmentRepository(fixture)
    result = process_pending_mail(store, repo, settings)
    replay = process_pending_mail(store, repo, settings)
    print(json.dumps({"isolated": True, "sample_mode": True, "authentication": "fixture_only",
                      "result": result, "replay": replay,
                      "applications": [{"id": a.id, "stage": a.stage.value} for a in repo.list_applications()]},
                     ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
