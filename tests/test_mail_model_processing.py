import json
from types import SimpleNamespace
from datetime import datetime, timezone

from packages.domain.models import Application, ApplicationStage
from packages.recruitment_mail import EmailMessage, MailIdentity, RecruitmentMailStore
from packages.storage import Storage, ApplicationSnapshot
from packages.recruitment_mail.processing import process_pending_mail
from packages.recruitment_mail.analysis_store import get_model_analysis


class Client:
    model = "fixture-model"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(content=json.dumps(next(self.responses)), model=self.model)


class Repo:
    def __init__(self, application):
        self.application = application

    def list_applications(self):
        return [self.application]

    def get_job(self, job_id):
        return None


def setup_case(event="assessment"):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:")
    store = RecruitmentMailStore(storage)
    body = "感谢参加 DJI 大疆校园招聘并投递AI DevOps 工程师（深圳），现诚邀您参与在线测评！"
    if event == "written_test":
        body = body.replace("在线测评", "技术笔试")
    record = store.upsert(EmailMessage(
        identity=MailIdentity(message_id="model-dji"), sender="recruitment",
        subject="来自DJI 大疆的测评邀请", body_text=body,
        received_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
        source_metadata={"authentication_results": [{
            "method": "dkim", "result": "pass", "authserv_id": "163.com", "aligned": True,
        }]},
    ), source="imap_readonly")
    app = Application(id="4", company_name="DJI 大疆", job_title="AI DevOps 工程师（深圳）",
                      stage=ApplicationStage.APPLIED, source="fixture", source_ref="4", idempotency_key="app4")
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="4", company_name=app.company_name, job_title=app.job_title,
                                       stage="applied", stage_history=[], source="fixture", source_ref="4",
                                       idempotency_key="app4"))
    proposal = dict(record_id=record.id, content_digest=record.content_digest,
                    company_name="DJI 大疆", job_title=app.job_title, job_code=None,
                    event_type=event, event_time=None, deadline=None, evidence_quotes=[body],
                    candidate_application_id="4", match_reason="Exact company and title", action_summary=None)
    triage = [dict(record_id=record.id, content_digest=record.content_digest,
                   relevance="relevant", reason="Assessment invitation")]
    settings = SimpleNamespace(write_enabled=True, llm_enabled=True)
    return store, Repo(app), record, settings, triage, proposal


def test_model_dji_overrides_wrong_legacy_extraction_and_second_run_skips():
    store, repo, record, settings, triage, proposal = setup_case()
    client = Client([triage, proposal])
    result = process_pending_mail(store, repo, settings, client=client)
    assert result["unchanged"] == 1
    assert store.get(record.id).application_id == "4"
    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, "4").stage == "applied"
    assert get_model_analysis(store, record.id)["model"] == "fixture-model"
    saved = store.get(record.id)
    assert saved.category == "assessment"
    assert saved.parsed_result["category"] == "assessment"
    assert saved.parsed_result["job_candidates"][0]["value"] == proposal["job_title"]
    assert saved.pending_confirmation_reasons == []
    assert process_pending_mail(store, repo, settings, client=client)["processed"] == 0
    assert client.calls == 2


def test_reset_analysis_cache_supports_processing_and_mail_detail():
    from packages.recruitment_mail.storage import RecruitmentMailRecord
    from packages.recruitment_mail.record_view import parsed_record
    store, repo, record, settings, triage, proposal = setup_case()
    with store.storage.write_transaction() as session:
        session.get(RecruitmentMailRecord, record.id).parsed_result = {}
    view = parsed_record(store.get(record.id))
    assert view.identity.message_id == record.message_id
    assert view.body_text == record.body_text
    assert view.safety_flags == record.safety_flags
    client = Client([triage, proposal])
    result = process_pending_mail(store, repo, settings, client=client)
    assert result["unchanged"] == 1
    assert result["schedule_items_created"] == 1
    assert client.calls == 2
    assert store.get(record.id).parsed_result["category"] == "assessment"
    assert process_pending_mail(store, repo, settings, client=client)["processed"] == 0


def test_reset_analysis_cache_supports_irrelevant_triage():
    from packages.recruitment_mail.storage import RecruitmentMailRecord
    store, repo, record, settings, triage, proposal = setup_case()
    with store.storage.write_transaction() as session:
        session.get(RecruitmentMailRecord, record.id).parsed_result = {}
    triage[0].update(relevance="irrelevant", reason="Notification")
    result = process_pending_mail(store, repo, settings, client=Client([triage]))
    assert result["irrelevant"] == 1
    assert store.get(record.id).parsed_result["category"] == "other"


def test_irrelevant_mail_is_persisted_without_full_analysis():
    store, repo, record, settings, triage, proposal = setup_case()
    triage[0].update(relevance="irrelevant", reason="Fixture classification")
    client = Client([triage])
    result = process_pending_mail(store, repo, settings, client=client)
    assert result["irrelevant"] == 1
    assert store.get(record_id=record.id).processing_status == "irrelevant"
    assert store.get(record.id).pending_confirmation_reasons == []
    assert client.calls == 1


def test_receipt_needs_no_binding_and_is_not_unchanged():
    store, repo, record, settings, triage, proposal = setup_case("application_confirmation")
    proposal.update(candidate_application_id=None, company_name=None, job_title=None)
    result = process_pending_mail(store, repo, settings, client=Client([triage, proposal]))
    assert result["notifications"] == 1 and result["unchanged"] == 0
    assert result["unresolved"] == 0 and result["scope_complete"] is True
    assert store.get(record.id).application_id is None
    assert store.get(record.id).processing_status == "processed"


def test_action_and_company_assessment_are_reminders():
    for event in ("action_required", "assessment"):
        store, repo, record, settings, triage, proposal = setup_case(event)
        proposal.update(candidate_application_id=None, job_title=None, action_summary="完成测评")
        result = process_pending_mail(store, repo, settings, client=Client([triage, proposal]))
        assert result["reminders"] == 1 and result["unchanged"] == 0
        assert store.get(record.id).application_id is None


def test_company_aliases_accept_legal_suffix_but_not_arbitrary_substrings():
    from packages.recruitment_mail.identity import company_names_match
    assert company_names_match("达梦", "武汉达梦数据库股份有限公司")
    assert company_names_match("奇瑞汽车", "奇瑞汽车股份有限公司")
    assert company_names_match("示例科技", "示例科技有限公司")
    assert not company_names_match("示例", "示例科技有限公司")
    assert not company_names_match("达梦", "其他达梦科技有限公司")


def test_explicit_written_test_still_advances_to_written():
    store, repo, record, settings, triage, proposal = setup_case("written_test")
    result = process_pending_mail(store, repo, settings, client=Client([triage, proposal]))
    assert result["updated"] == 1
    assert store.get(record.id).category == "written_test"
    assert store.get(record.id).application_id == "4"
    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, "4").stage == "written"


def test_label_backfill_is_idempotent_and_preserves_status_and_transport():
    from packages.recruitment_mail.analysis_store import save_model_analysis, sync_analysis_labels
    store, repo, record, settings, triage, proposal = setup_case()
    save_model_analysis(store, record.id, record.content_digest, "fixture", proposal, "proposed")
    transport = store.get(record.id).raw_metadata.get("transport")
    for _ in range(2):
        assert sync_analysis_labels(store, record.id)
        saved = store.get(record.id)
        assert saved.category == "assessment"
        assert saved.processing_status == "pending"
        assert saved.application_id is None
        assert saved.raw_metadata.get("transport") == transport
        with store.storage.session() as session:
            assert session.get(ApplicationSnapshot, "4").stage == "applied"
    with store.storage.write_transaction() as session:
        from packages.recruitment_mail.storage import RecruitmentMailRecord
        session.get(RecruitmentMailRecord, record.id).content_digest = "a" * 64
    assert not sync_analysis_labels(store, record.id)


def test_invented_quote_fails_and_does_not_retry():
    store, repo, record, settings, triage, proposal = setup_case()
    proposal["evidence_quotes"] = ["nonexistent quote"]
    client = Client([triage, proposal])
    result = process_pending_mail(store, repo, settings, client=client)
    assert result["failed"] == 1
    assert process_pending_mail(store, repo, settings, client=client)["processed"] == 0
    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, "4").stage == "applied"


def test_duplicate_company_jobs_do_not_write():
    store, repo, record, settings, triage, proposal = setup_case()
    a = repo.application
    repo.list_applications = lambda: [a, a.model_copy(update={"id": "5"})]
    client = Client([triage, proposal])
    result = process_pending_mail(store, repo, settings, client=client)
    assert result["unresolved"] == 1
    assert result["results"][0]["reason"] == "multiple_candidates"


def test_read_only_configuration_never_calls_model():
    store, repo, record, settings, triage, proposal = setup_case()
    settings.write_enabled = False
    client = Client([])
    assert process_pending_mail(store, repo, settings, client=client)["status"] == "blocked"
    assert client.calls == 0


def test_json_envelope_does_not_allow_extra_prose_or_partial_objects():
    import pytest
    from packages.recruitment_mail.processing import _decode_model_json

    assert _decode_model_json('```json\n{"event_type":"assessment"}\n```') == {"event_type": "assessment"}
    for content in ['Answer: {"event_type":"assessment"}', '{"event_type":', '{} {}']:
        with pytest.raises(json.JSONDecodeError):
            _decode_model_json(content)


def test_fingerprint_is_not_changed_by_model_proposal():
    from packages.recruitment_mail.processing import _input_digest
    store, repo, record, settings, triage, proposal = setup_case()
    before = _input_digest(record, repo.list_applications())
    record.raw_metadata = dict(record.raw_metadata, model_analysis={"payload": proposal})
    assert _input_digest(record, repo.list_applications()) == before


def test_schema_diagnostics_do_not_expose_model_values():
    from pydantic import ValidationError
    from packages.recruitment_mail.processing import _failure_diagnostic
    from packages.recruitment_mail.model_analysis import MailAnalysisProposal
    try:
        MailAnalysisProposal.model_validate({"private_unknown_key": "secret-value"})
    except ValidationError as exc:
        diagnostic = json.dumps(_failure_diagnostic(exc))
        assert "secret-value" not in diagnostic
        assert "private_unknown_key" not in diagnostic


def test_single_correction_precedes_write_and_does_not_repeat_processed_mail():
    store, repo, record, settings, triage, proposal = setup_case()
    invalid = dict(proposal, evidence_quotes=["invented"])
    client = Client([triage, invalid, proposal])
    result = process_pending_mail(store, repo, settings, client=client)
    assert result["unchanged"] == 1
    assert client.calls == 3
    assert process_pending_mail(store, repo, settings, client=client)["processed"] == 0
    assert client.calls == 3


def test_authentication_backfill_metadata_does_not_trigger_another_model_attempt(monkeypatch):
    from packages.tools import application_status_update as writer
    store, repo, record, settings, triage, proposal = setup_case()
    def blocked(*args, **kwargs):
        store.update_source_metadata(record.id, {"sender_domain": "dji.com", "authentication_results": []},
                                     processing_status="needs_auth_metadata", backfill_outcome="no_aligned_dkim")
        return SimpleNamespace(success=False, model_dump=lambda **kw: {"success": False})
    monkeypatch.setattr(writer, "update_application_status", blocked)
    client = Client([triage, proposal])
    assert process_pending_mail(store, repo, settings, client=client)["unresolved"] == 1
    assert process_pending_mail(store, repo, settings, client=client)["processed"] == 0
    assert client.calls == 2


def test_extra_model_fields_cannot_grant_authentication_or_skip_evidence():
    from packages.recruitment_mail.processing import _analysis_proposal
    store, repo, record, settings, triage, proposal = setup_case()
    value = dict(proposal, verified=True, authentication_results=[{"result": "pass"}], relevance="relevant")
    parsed = _analysis_proposal(json.dumps(value))
    assert parsed.model_dump() == _analysis_proposal(json.dumps(proposal)).model_dump()
    value["evidence_quotes"] = ["made up"]
    result = process_pending_mail(store, repo, settings, client=Client([triage, value, value]))
    assert result["failed"] == 1
    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, "4").stage == "applied"


def test_structured_evidence_ids_bind_exact_source_without_model_retyping():
    from packages.recruitment_mail.processing import _analysis_proposal
    import pytest
    store, repo, record, settings, triage, proposal = setup_case()
    spans = {"body_0": record.body_text}
    response = dict(proposal, evidence_ids=["body_0"], evidence_quotes=["model paraphrase"])
    assert _analysis_proposal(json.dumps(response), spans).evidence_quotes == [record.body_text]
    response["evidence_ids"] = ["invented"]
    with pytest.raises(ValueError, match="invalid_evidence_ids"):
        _analysis_proposal(json.dumps(response), spans)
