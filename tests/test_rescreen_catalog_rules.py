from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event

from packages.matching.rules import content_fingerprint, profile_fingerprint
from packages.storage import (
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
    Storage,
    create_storage_engine,
    initialize_schema,
)
from scripts.rescreen_catalog_rules import (
    RescreenTransactionError,
    _Report,
    _apply_plan,
    _scan,
    rescreen_catalog,
)


PROFILE = {
    "degree": "硕士",
    "job_type": "校招",
    "direction": "C++软件开发",
    "skills": ["C++", "Python"],
    "matching": {
        "direction_policy": "parallel",
        "primary_directions": ["C++软件开发"],
        "secondary_directions": [],
        "project_evidence": ["C++机器人软件项目"],
        "supporting_skills": ["Linux"],
        "learning_targets": ["ROS2"],
        "unverified_skills": ["SLAM"],
    },
}
BASE_TIME = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
COMPLETE_JD = (
    "岗位职责：负责 C++ 系统软件开发和测试，参与技术方案设计、编码实现、性能优化、"
    "问题定位和工程协作，维护线上系统稳定运行。\n"
    "任职要求：熟悉 C++，有 C++机器人软件项目经验，能够在 Linux 环境开发，"
    "具备良好的编程基础、文档阅读能力和团队协作能力。"
)


@pytest.fixture
def storage() -> Storage:
    engine = create_storage_engine("sqlite:///:memory:")
    initialize_schema(engine)
    value = Storage(engine)
    try:
        yield value
    finally:
        engine.dispose()


def _job(job_id: str, *, jd_raw: str = COMPLETE_JD, batch: str = "formal") -> dict[str, Any]:
    capture_evidence = (
        {
            "status": "complete",
            "method": "test_fixture",
            "source_url": f"https://example.com/jobs/{job_id}",
            "identity_verified": True,
            "terminal_observed": True,
            "remaining_controls": [],
            "content_sha256": sha256(jd_raw.encode("utf-8")).hexdigest(),
        }
        if jd_raw == COMPLETE_JD
        else {}
    )
    return {
        "id": job_id,
        "company": "示例公司",
        "company_id": "company-1",
        "title": "C++软件开发工程师",
        "city": "上海",
        "detail_url": f"https://example.com/jobs/{job_id}",
        "jd_raw": jd_raw,
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": batch,
        "source": "fixture",
        "source_ref": f"fixture:job:{job_id}",
        "capture_evidence": capture_evidence,
    }


def _old_analysis(
    payload: dict[str, Any],
    *,
    status: str = "eligible",
    model: str | None = None,
    score: int | None = None,
) -> JobAnalysisSnapshot:
    return JobAnalysisSnapshot(
        job_id=payload["id"],
        source="fixture",
        source_ref=f"{payload['source_ref']}:analysis",
        match_score=score,
        advantages='["保留旧优势"]',
        gaps='["保留旧差距"]',
        summary="旧说明",
        recommendation="旧推荐",
        score_breakdown={"old": 1},
        evidence=[{"old": True}],
        evidence_level="partial",
        matched_directions=["cpp_software"],
        primary_match_direction="cpp_software",
        analysis_status=status,
        model=model,
        analysis_version="old-version",
        prompt_version="old-prompt",
        content_fingerprint=content_fingerprint(payload),
        profile_fingerprint=profile_fingerprint(PROFILE),
        filter_reasons=["old-reason"],
        refusal_reason="old-refusal",
        error_code="old-error",
        analyzed_at=BASE_TIME,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )


def _seed(
    storage: Storage,
    payloads: list[dict[str, Any]],
    analyses: list[JobAnalysisSnapshot] | None = None,
) -> None:
    by_id = {item.job_id: item for item in analyses or []}
    with storage.write_transaction() as session:
        session.add(
            CompanySnapshot(
                id="company-1",
                name="示例公司",
                aliases=[],
                integration_status="connected",
                source="fixture",
                source_ref="fixture:company-1",
            )
        )
        for payload in payloads:
            session.add(
                JobSnapshot(
                    id=payload["id"],
                    company_id=payload["company_id"],
                    title=payload["title"],
                    city=payload["city"],
                    detail_url=payload["detail_url"],
                    jd_raw=payload["jd_raw"],
                    cohort=payload["cohort"],
                    cohort_status=payload["cohort_status"],
                    batch=payload["batch"],
                    match_score=(by_id[payload["id"]].match_score if payload["id"] in by_id else None),
                    first_seen_at=BASE_TIME,
                    last_seen_at=BASE_TIME,
                    source=payload["source"],
                    source_ref=payload["source_ref"],
                    created_at=BASE_TIME,
                    updated_at=BASE_TIME,
                    capture_evidence=payload["capture_evidence"],
                )
            )
        session.flush()
        for analysis in analyses or []:
            session.add(analysis)


def _backup_manifest(tmp_path: Path, payloads: list[dict[str, Any]]) -> dict[str, Any]:
    backup = tmp_path / "catalog.dump"
    backup.write_bytes(b"isolated backup")
    jobs = []
    for payload in payloads:
        job = dict(payload)
        job["content_fingerprint"] = content_fingerprint(job)
        jobs.append(job)
    return {
        "run_id": "rescreen-test",
        "profile": PROFILE,
        "profile_fingerprint": profile_fingerprint(PROFILE),
        "jobs": jobs,
        "backup": {"path": str(backup), "sha256": sha256(backup.read_bytes()).hexdigest()},
    }


def _analysis(storage: Storage, job_id: str) -> JobAnalysisSnapshot | None:
    with storage.session() as session:
        return session.get(JobAnalysisSnapshot, job_id)


def test_rescreen_moves_old_rule_states_and_dry_run_is_read_only(
    storage: Storage, tmp_path: Path
) -> None:
    eligible_to_jd = _job("eligible-to-jd", jd_raw="岗位职责：负责 C++ 软件开发。")
    jd_to_eligible = _job("jd-to-eligible")
    early = _job("early", batch="early_batch")
    new_job = _job("new")
    complete = _job("complete")
    luna_score = _job("luna-score")
    luna_exclude = _job("luna-exclude")
    luna_defer = _job("luna-defer", jd_raw="岗位职责：负责 C++ 软件开发。")
    payloads = [
        eligible_to_jd,
        jd_to_eligible,
        early,
        new_job,
        complete,
        luna_score,
        luna_exclude,
        luna_defer,
    ]
    analyses = [
        _old_analysis(eligible_to_jd, status="eligible"),
        _old_analysis(jd_to_eligible, status="jd_incomplete"),
        _old_analysis(early, status="early_batch"),
        _old_analysis(complete, status="complete"),
        _old_analysis(luna_score, status="complete", model="gpt-5.6-luna", score=88),
        _old_analysis(luna_exclude, status="direction_out", model="gpt-5.6-luna"),
        _old_analysis(luna_defer, status="jd_incomplete", model="gpt-5.6-luna"),
    ]
    _seed(storage, payloads, analyses)

    before = _analysis(storage, "eligible-to-jd")
    report = rescreen_catalog(storage, PROFILE)

    assert report["dry_run"] is True
    assert report["written"] == 0
    assert report["planned"] == 4
    assert report["skipped"] == 4
    assert report["conflicts"] == 0
    assert report["rule_status_counts"] == {"eligible": 3, "jd_incomplete": 1}
    assert _analysis(storage, "eligible-to-jd").summary == before.summary
    assert _analysis(storage, "new") is None

    manifest = _backup_manifest(tmp_path, payloads)
    applied = rescreen_catalog(storage, PROFILE, apply=True, manifest=manifest)

    assert applied["written"] == 4
    assert applied["reused"] == 0
    assert _analysis(storage, "eligible-to-jd").analysis_status == "jd_incomplete"
    assert _analysis(storage, "jd-to-eligible").analysis_status == "eligible"
    assert _analysis(storage, "early").analysis_status == "eligible"
    assert _analysis(storage, "new").analysis_status == "eligible"
    assert _analysis(storage, "new").match_score is None
    assert _analysis(storage, "luna-score").match_score == 88
    assert _analysis(storage, "luna-exclude").model == "gpt-5.6-luna"
    assert _analysis(storage, "luna-defer").analysis_status == "jd_incomplete"


def test_noncomplete_score_is_a_conflict_and_complete_or_model_rows_are_protected(
    storage: Storage, tmp_path: Path
) -> None:
    conflict = _job("conflict")
    complete = _job("complete")
    modelled = _job("modelled")
    analyses = [
        _old_analysis(conflict, status="eligible", score=42),
        _old_analysis(complete, status="complete"),
        _old_analysis(modelled, status="eligible", model="gpt-5.6-luna"),
    ]
    payloads = [conflict, complete, modelled]
    _seed(storage, payloads, analyses)
    original = {job_id: _analysis(storage, job_id).summary for job_id in ("conflict", "complete", "modelled")}

    report = rescreen_catalog(storage, PROFILE)
    assert report["planned"] == 0
    assert report["skipped"] == 2
    assert report["conflicts"] == 1
    assert report["conflict_reasons"] == {"match_score_present": 1}

    applied = rescreen_catalog(
        storage,
        PROFILE,
        apply=True,
        manifest=_backup_manifest(tmp_path, payloads),
    )
    assert applied["written"] == 0
    assert applied["conflicts"] == 1
    assert {job_id: _analysis(storage, job_id).summary for job_id in original} == original
    assert _analysis(storage, "conflict").match_score == 42


def test_apply_is_idempotent_and_does_not_touch_updated_at_on_reuse(
    storage: Storage, tmp_path: Path
) -> None:
    payload = _job("idempotent")
    _seed(storage, [payload], [_old_analysis(payload, status="early_batch")])
    manifest = _backup_manifest(tmp_path, [payload])

    first = rescreen_catalog(storage, PROFILE, apply=True, manifest=manifest)
    first_updated_at = _analysis(storage, payload["id"] ).updated_at
    second = rescreen_catalog(storage, PROFILE, apply=True, manifest=manifest)

    assert first["written"] == 1
    assert second["planned"] == 1
    assert second["written"] == 0
    assert second["reused"] == 1
    assert _analysis(storage, payload["id"]).updated_at == first_updated_at


@pytest.mark.parametrize("race", ["model", "score"])
def test_locked_reread_protects_concurrent_model_or_score(
    tmp_path: Path, race: str
) -> None:
    database = tmp_path / f"race-{race}.sqlite"
    first_engine = create_storage_engine(f"sqlite+pysqlite:///{database}")
    initialize_schema(first_engine)
    first_storage = Storage(first_engine)
    second_storage = Storage.from_url(f"sqlite+pysqlite:///{database}")
    payload = _job(f"race-{race}")
    _seed(first_storage, [payload], [_old_analysis(payload, status="eligible")])

    try:
        with first_storage.session() as session:
            report = _Report(dry_run=False)
            plans = _scan(session, PROFILE, profile_fingerprint(PROFILE), report)
            assert len(plans) == 1
            stale_analysis = session.get(JobAnalysisSnapshot, payload["id"])
            session.commit()

            with second_storage.write_transaction() as writer:
                writer.get(JobSnapshot, payload["id"]).source = "concurrent-source"
                live_analysis = writer.get(JobAnalysisSnapshot, payload["id"])
                if race == "model":
                    live_analysis.model = "gpt-5.6-luna"
                    live_analysis.analysis_status = "complete"
                    live_analysis.match_score = 91
                else:
                    live_analysis.match_score = 91
                    writer.get(JobSnapshot, payload["id"]).match_score = 91

            outcome = _apply_plan(
                session,
                plans[0],
                PROFILE,
                profile_fingerprint(PROFILE),
                report,
            )
            assert outcome == ("skipped" if race == "model" else "conflict")
            assert stale_analysis.model == ("gpt-5.6-luna" if race == "model" else None)
            assert stale_analysis.match_score == 91
            assert session.get(JobSnapshot, payload["id"]).source == "concurrent-source"
            session.rollback()
    finally:
        second_storage.engine.dispose()
        first_storage.engine.dispose()


def test_apply_rolls_back_all_rows_when_one_insert_fails(
    storage: Storage, tmp_path: Path
) -> None:
    first = _job("rollback-first")
    second = _job("rollback-second")
    payloads = [first, second]
    _seed(storage, payloads)
    manifest = _backup_manifest(tmp_path, payloads)

    def fail_on_second(_mapper: Any, _connection: Any, target: JobAnalysisSnapshot) -> None:
        if target.job_id == second["id"]:
            raise RuntimeError("forced failure")

    event.listen(JobAnalysisSnapshot, "before_insert", fail_on_second)
    try:
        with pytest.raises(RescreenTransactionError, match="transaction_rolled_back"):
            rescreen_catalog(storage, PROFILE, apply=True, manifest=manifest)
    finally:
        event.remove(JobAnalysisSnapshot, "before_insert", fail_on_second)

    assert _analysis(storage, first["id"]) is None
    assert _analysis(storage, second["id"]) is None


def test_apply_rejects_manifest_profile_mismatch(storage: Storage, tmp_path: Path) -> None:
    payload = _job("profile-guard")
    _seed(storage, [payload])
    manifest = _backup_manifest(tmp_path, [payload])
    manifest["profile_fingerprint"] = profile_fingerprint({"changed": True})

    with pytest.raises(ValueError, match="manifest_profile_fingerprint_mismatch"):
        rescreen_catalog(storage, PROFILE, apply=True, manifest=manifest)
