from __future__ import annotations

from hashlib import sha256
import json

import pytest
from sqlalchemy import delete, event, select, update

from packages.domain.models import Company, Job, JobAnalysis, RecruitmentBatch
from packages.matching import DeepSeekResponse, MatchingService
from packages.matching.rules import profile_fingerprint
from packages.storage import JobAnalysisSnapshot, JobSnapshot, Storage
from packages.storage.sync import upsert_company_snapshot, upsert_job_analysis_snapshot, upsert_job_snapshot
from scripts import repair_legacy_matching_scores as repair_script


PROFILE = {"degree": "硕士", "skills": ["C++", "Linux"], "matching": {
    "primary_directions": ["C++软件开发"], "project_evidence": ["C++机器人软件项目"],
    "supporting_skills": ["Linux"],
}}


def _storage(*ids: str) -> Storage:
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    detail = "岗位职责：负责 C++ 核心软件开发、测试和性能优化。任职要求：熟悉 C++、Linux 和多线程开发。"
    with storage.transaction(write=True) as session:
        upsert_company_snapshot(session, Company(id="company", name="示例公司", integration_status="connected",
                                                 source="test", source_ref="company"))
        for identifier in ids or ("affected",):
            job = Job(id=identifier, company_id="company", title="C++软件开发工程师",
                      detail_url=f"https://example.com/{identifier}", jd_raw=detail,
                      cohort=2027, cohort_status="confirmed", batch=RecruitmentBatch.FORMAL,
                      match_score=38, source="test", source_ref=identifier, capture_evidence={
                          "status": "complete", "method": "test_fixture", "identity_verified": True,
                          "terminal_observed": True, "remaining_controls": [],
                          "source_url": f"https://example.com/{identifier}",
                          "content_sha256": sha256(detail.encode()).hexdigest(),
                      })
            upsert_job_snapshot(session, job)
            upsert_job_analysis_snapshot(session, job, JobAnalysis(
                match_score=38, score_breakdown={"core_direction": 0, "required_skills": 0,
                                               "project_evidence": 24, "engineering_stack": 14},
                analysis_status="complete", prompt_version="matching-prompt-v1",
                model="deepseek-flash", profile_fingerprint=profile_fingerprint(PROFILE), evidence=[],
            ))
    return storage


class FakeClient:
    model = "deepseek-flash"

    def __init__(self, callback=None, invalid=False):
        self.calls = 0
        self.callback = callback
        self.invalid = invalid

    def complete(self, **kwargs):
        self.calls += 1
        if self.callback:
            self.callback()
        if self.invalid:
            return DeepSeekResponse(content="{}")
        return DeepSeekResponse(content=json.dumps({
            "matched_directions": ["cpp_software"], "primary_match_direction": "cpp_software",
            "score_breakdown": {"core_direction": 25, "required_skills": 25,
                                "project_evidence": 20, "engineering_stack": 10},
            "evidence_level": "partial", "evidence": [{"jd_requirement": "C++",
                "profile_evidence": "C++机器人软件项目", "relation": "direct", "requirement_type": "core"}],
            "summary": "方向和技能匹配", "missing_core_requirements": [], "advantages": [], "gaps": [],
        }, ensure_ascii=False))


def _row(storage, table, column, identifier="affected"):
    with storage.engine.connect() as connection:
        return dict(connection.execute(select(table).where(column == identifier)).mappings().one())


def test_plan_only_targets_old_model_contract_zero_dimensions_and_broken_evidence():
    storage = _storage("affected", "normal_low", "other_model", "new_prompt", "real_zero", "old_profile", "failed")
    table = JobAnalysisSnapshot.__table__
    with storage.engine.begin() as connection:
        connection.execute(update(table).where(table.c.job_id == "normal_low").values(
            score_breakdown={"core_direction": 5, "required_skills": 4, "project_evidence": 0, "engineering_stack": 0}))
        connection.execute(update(table).where(table.c.job_id == "other_model").values(model="gpt-test"))
        connection.execute(update(table).where(table.c.job_id == "new_prompt").values(prompt_version="matching-prompt-v3"))
        connection.execute(update(table).where(table.c.job_id == "real_zero").values(evidence=[{
            "jd_requirement": "C++", "profile_evidence": "C++项目", "relation": "direct", "requirement_type": "core"}]))
        connection.execute(update(table).where(table.c.job_id == "old_profile").values(profile_fingerprint="a" * 64))
        connection.execute(update(table).where(table.c.job_id == "failed").values(analysis_status="failed"))
    selected, counts = repair_script.build_plan(storage, PROFILE)
    assert [item.job["id"] for item in selected] == ["affected"]
    assert counts["skipped_profile_changed"] == 1
    assert counts["skipped_evidence_not_incident"] == 1
    selected, _ = repair_script.build_plan(storage, PROFILE, allow_profile_change=True)
    assert [item.job["id"] for item in selected] == ["affected", "old_profile"]


def test_plan_skips_unavailable_incomplete_and_ineligible():
    storage = _storage("inactive", "incomplete", "ineligible", "good")
    table = JobSnapshot.__table__
    with storage.engine.begin() as connection:
        connection.execute(update(table).where(table.c.id == "inactive").values(availability_status="inactive"))
        connection.execute(update(table).where(table.c.id == "incomplete").values(capture_evidence={}))
        connection.execute(update(table).where(table.c.id == "ineligible").values(title="销售经理"))
    selected, counts = repair_script.build_plan(storage, PROFILE)
    assert [item.job["id"] for item in selected] == ["good"]
    assert counts["skipped_unavailable"] == counts["skipped_incomplete_jd"] == counts["skipped_ineligible"] == 1


def test_plan_is_bounded_and_does_not_write():
    storage = _storage("a", "b")
    before = _row(storage, repair_script.ANALYSIS, repair_script.ANALYSIS.c.job_id, "a")
    selected, counts = repair_script.build_plan(storage, PROFILE, limit=1)
    assert len(selected) == 1 and counts["eligible"] == 2
    assert before == _row(storage, repair_script.ANALYSIS, repair_script.ANALYSIS.c.job_id, "a")
    with pytest.raises(ValueError):
        repair_script.build_plan(storage, PROFILE, limit=201)


@pytest.mark.parametrize("mutation", ["v3_score", "model", "status", "no_score", "direction", "skills", "boolean_zero", "delete_analysis", "delete_job"])
def test_plan_rechecks_incident_after_id_selection_race(mutation):
    storage = _storage()
    armed = False
    mutated = False

    def mutate_after_id_selection(connection, _cursor, statement, _parameters, _context, _many):
        nonlocal armed, mutated
        if "JOIN job_analysis_snapshots" in statement:
            armed = True
        elif armed and not mutated and statement.startswith("SELECT job_snapshots.id,"):
            mutated = True
            table = repair_script.ANALYSIS
            if mutation == "delete_analysis":
                connection.execute(delete(table))
            elif mutation == "delete_job":
                connection.execute(delete(repair_script.JOB))
            else:
                values = {
                    "v3_score": {"prompt_version": "matching-prompt-v3", "match_score": 0},
                    "model": {"model": "another-model"},
                    "status": {"analysis_status": "failed"},
                    "no_score": {"match_score": None},
                    "direction": {"score_breakdown": {"core_direction": 20, "required_skills": 0}},
                    "skills": {"score_breakdown": {"core_direction": 0, "required_skills": 20}},
                    "boolean_zero": {"score_breakdown": {"core_direction": False, "required_skills": 0}},
                }[mutation]
                connection.execute(update(table).values(**values))

    event.listen(storage.engine, "before_cursor_execute", mutate_after_id_selection)
    try:
        selected, counts = repair_script.build_plan(storage, PROFILE)
    finally:
        event.remove(storage.engine, "before_cursor_execute", mutate_after_id_selection)
    assert mutated
    assert selected == []
    assert counts["suspected"] == counts["skipped_concurrent_change"] == 1


def test_repair_backs_up_original_before_model_and_updates_both_rows(tmp_path):
    storage = _storage()
    selected, _ = repair_script.build_plan(storage, PROFILE)
    def check_backup():
        event = json.loads((tmp_path / "repair.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert event["event"] == "original"
        assert event["analysis"]["match_score"] == event["job"]["match_score"] == 38
        assert event["profile_fingerprint"] == profile_fingerprint(PROFILE)
    client = FakeClient(callback=check_backup)
    result = repair_script.repair(storage, PROFILE, MatchingService(client), selected, state_dir=tmp_path)
    assert result["applied"] == 1 and client.calls == 1
    assert _row(storage, repair_script.JOB, repair_script.JOB.c.id)["match_score"] == 80
    analysis = _row(storage, repair_script.ANALYSIS, repair_script.ANALYSIS.c.job_id)
    assert analysis["match_score"] == 80
    assert analysis["prompt_version"] != "matching-prompt-v1"
    assert repair_script.build_plan(storage, PROFILE)[0] == []


def test_invalid_output_preserves_exact_old_rows(tmp_path):
    storage = _storage()
    selected, _ = repair_script.build_plan(storage, PROFILE)
    client = FakeClient(invalid=True)
    result = repair_script.repair(storage, PROFILE, MatchingService(client), selected, state_dir=tmp_path)
    assert result["failed_preserved"] == 1
    assert selected[0].job == _row(storage, repair_script.JOB, repair_script.JOB.c.id)
    assert selected[0].analysis == _row(storage, repair_script.ANALYSIS, repair_script.ANALYSIS.c.job_id)


@pytest.mark.parametrize("change_analysis", [True, False])
def test_concurrent_row_change_prevents_overwrite(tmp_path, change_analysis):
    storage = _storage()
    selected, _ = repair_script.build_plan(storage, PROFILE)
    def concurrent_change():
        with storage.engine.begin() as connection:
            if change_analysis:
                connection.execute(update(repair_script.ANALYSIS).values(summary="concurrently edited"))
            else:
                connection.execute(update(repair_script.JOB).values(city="上海"))
    result = repair_script.repair(storage, PROFILE, MatchingService(FakeClient(callback=concurrent_change)),
                                  selected, state_dir=tmp_path)
    assert result["concurrent_change_skipped"] == 1
    assert _row(storage, repair_script.JOB, repair_script.JOB.c.id)["match_score"] == 38
    assert _row(storage, repair_script.ANALYSIS, repair_script.ANALYSIS.c.job_id)["match_score"] == 38


def test_resume_reuses_durable_proposal_after_interruption(tmp_path, monkeypatch):
    storage = _storage()
    selected, _ = repair_script.build_plan(storage, PROFILE)
    client = FakeClient()
    original_apply = repair_script.apply_if_unchanged
    def interrupted(*_args):
        raise RuntimeError("interrupted before write")
    monkeypatch.setattr(repair_script, "apply_if_unchanged", interrupted)
    with pytest.raises(RuntimeError):
        repair_script.repair(storage, PROFILE, MatchingService(client), selected, state_dir=tmp_path)
    monkeypatch.setattr(repair_script, "apply_if_unchanged", original_apply)
    result = repair_script.repair(storage, PROFILE, MatchingService(client), selected, state_dir=tmp_path)
    assert result["reused_proposal"] == result["applied"] == 1
    assert client.calls == 1


def test_backup_failure_prevents_model_or_database_write(tmp_path, monkeypatch):
    storage = _storage()
    selected, _ = repair_script.build_plan(storage, PROFILE)
    client = FakeClient()
    def disk_full(*_args):
        raise OSError("disk full")
    monkeypatch.setattr(repair_script.Journal, "append", disk_full)
    with pytest.raises(OSError):
        repair_script.repair(storage, PROFILE, MatchingService(client), selected, state_dir=tmp_path)
    assert client.calls == 0
    assert _row(storage, repair_script.JOB, repair_script.JOB.c.id)["match_score"] == 38


def test_cli_is_dry_run_by_default(tmp_path, monkeypatch, capsys):
    storage = _storage()
    from packages.config import Settings
    monkeypatch.setattr(repair_script, "load_instance", lambda *_args: (storage, Settings(), PROFILE))
    monkeypatch.setattr(repair_script, "DeepSeekClient", lambda **_kwargs: pytest.fail("dry run called model"))
    monkeypatch.setattr("sys.argv", ["repair", "--instance-root", str(tmp_path), "--db-port", "5432"])
    assert repair_script.main() == 0
    assert json.loads(capsys.readouterr().out)["apply"] is False
    assert not (tmp_path / "backups").exists()


def test_workers_are_bounded_and_journal_stays_parseable(tmp_path):
    from threading import Barrier, Lock
    storage = _storage("a", "b", "c", "d")
    selected, _ = repair_script.build_plan(storage, PROFILE)
    barrier = Barrier(2)
    lock = Lock()
    active = 0
    maximum = 0
    def concurrently_enter():
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
    result = repair_script.repair(storage, PROFILE, MatchingService(FakeClient(callback=concurrently_enter)),
                                  selected, state_dir=tmp_path, workers=2)
    assert result["applied"] == 4
    assert maximum == 2
    events = [json.loads(line) for line in (tmp_path / "repair.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len([event for event in events if event["event"] == "original"]) == 4
    assert len([event for event in events if event["event"] == "applied"]) == 4


def test_provider_stop_drains_only_current_wave_and_preserves_rows(tmp_path):
    from packages.matching.client import DeepSeekClientError
    storage = _storage("a", "b", "c", "d")
    selected, _ = repair_script.build_plan(storage, PROFILE)
    def quota_error():
        raise DeepSeekClientError("http_402")
    client = FakeClient(callback=quota_error)
    result = repair_script.repair(storage, PROFILE, MatchingService(client), selected, state_dir=tmp_path, workers=2)
    assert result["failed_preserved"] == result["provider_stop"] == client.calls == 2
    assert len(repair_script.build_plan(storage, PROFILE)[0]) == 4


def test_unexpected_model_exception_is_private_and_preserves_score(tmp_path):
    storage = _storage()
    selected, _ = repair_script.build_plan(storage, PROFILE)
    service = MatchingService(FakeClient())
    def fail(*_args, **_kwargs):
        raise RuntimeError("SECRET_CREDENTIAL_SHOULD_NOT_BE_LOGGED")
    service.analyze_title_first = fail
    result = repair_script.repair(storage, PROFILE, service, selected, state_dir=tmp_path)
    assert result["failed_preserved"] == 1
    assert "SECRET_CREDENTIAL" not in (tmp_path / "repair.jsonl").read_text(encoding="utf-8")
    assert _row(storage, repair_script.JOB, repair_script.JOB.c.id)["match_score"] == 38
