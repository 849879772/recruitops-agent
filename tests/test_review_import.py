from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select

from packages.matching.review_import import (
    ReviewImportTransactionError,
    _validate_evidence,
    import_reviewed_scores,
)
from packages.matching.models import DeepSeekMatchPayload, DeepSeekResponse
from packages.matching.rules import content_fingerprint, profile_fingerprint
from packages.matching.service import MatchingService
from packages.pipeline.daily import _existing_analysis_mapping, _stored_analysis_strings
from packages.storage import (
    ApplicationSnapshot,
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
    Storage,
    create_storage_engine,
    initialize_schema,
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
        "project_evidence": ["C++机器人软件项目", "RAG Agent项目"],
        "supporting_skills": ["Linux"],
        "learning_targets": ["ROS2"],
        "unverified_skills": ["SLAM"],
    },
}

BASE_TIME = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def _capture_evidence(detail: str, source_url: str) -> dict[str, Any]:
    return {
        "status": "complete",
        "method": "test_fixture",
        "source_url": source_url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
    }


def test_evidence_headings_and_repeated_requirements_cannot_fake_coverage():
    raw = _analysis()
    raw['evidence'][0]['jd_requirement'] = '岗位职责'
    payload = DeepSeekMatchPayload.model_validate(raw)
    assert _validate_evidence(payload, jd_raw=_job_payload()['jd_raw'], profile=PROFILE).endswith('heading_not_requirement')
    raw = _analysis()
    raw['evidence'] = [
        {'jd_requirement': 'C++', 'profile_evidence': 'C++', 'relation': 'direct', 'requirement_type': 'basic'},
        {'jd_requirement': 'C++', 'profile_evidence': 'C++机器人软件项目', 'relation': 'adjacent', 'requirement_type': 'core'},
    ]
    payload = DeepSeekMatchPayload.model_validate(raw)
    assert _validate_evidence(payload, jd_raw=_job_payload()['jd_raw'], profile=PROFILE).endswith('two_distinct_requirements_required')


@pytest.fixture
def storage() -> Storage:
    engine = create_storage_engine("sqlite:///:memory:")
    initialize_schema(engine)
    value = Storage(engine)
    try:
        yield value
    finally:
        engine.dispose()


def _job_payload(
    job_id: str = "job-1",
    *,
    title: str = "C++软件开发工程师",
    jd_raw: str | None = None,
    batch: str = "formal",
) -> dict[str, Any]:
    detail = jd_raw or (
        "岗位职责：负责 C++ 系统软件开发和测试，参与技术方案设计、编码实现、"
        "性能优化、问题定位和工程协作，维护线上系统稳定运行。\n"
        "任职要求：熟悉 C++，有 C++机器人软件项目经验，能够在 Linux 环境开发，"
        "具备良好的编程基础、文档阅读能力和团队协作能力。"
    )
    detail_url = f"https://example.com/jobs/{job_id}"
    return {
        "id": job_id,
        "company": "示例公司",
        "company_id": "company-1",
        "title": title,
        "city": "上海",
        "detail_url": detail_url,
        "jd_raw": detail,
        "capture_evidence": _capture_evidence(detail, detail_url),
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": batch,
        "source": "recruitops-agent.daily_pipeline",
        "source_ref": f"recruitops-agent.daily_pipeline:job:company-1:{job_id}",
    }


def _seed_job(
    storage: Storage,
    payload: dict[str, Any],
    *,
    old_analysis: dict[str, Any] | None = None,
    application: bool = True,
) -> None:
    with storage.write_transaction() as session:
        session.add(
            CompanySnapshot(
                id=payload["company_id"],
                name=payload["company"],
                aliases=[],
                integration_status="connected",
                source="fixture",
                source_ref="company-1",
            )
        )
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
                first_seen_at=BASE_TIME,
                last_seen_at=BASE_TIME,
                source=payload["source"],
                source_ref=payload["source_ref"],
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
                match_score=(old_analysis or {}).get("match_score"),
                capture_evidence=payload["capture_evidence"],
            )
        )
        session.flush()
        if old_analysis is not None:
            session.add(JobAnalysisSnapshot(**old_analysis))
        if application:
            session.add(
                ApplicationSnapshot(
                    id=f"application-{payload['id']}",
                    company_name=payload["company"],
                    job_title=payload["title"],
                    job_id=payload["id"],
                    record_url=payload["detail_url"],
                    stage="applied",
                    idempotency_key=f"application:{payload['id']}",
                    note="unchanged",
                    stage_history=[{"stage": "applied"}],
                    source="fixture",
                    source_ref=f"application:{payload['id']}",
                    created_at=BASE_TIME,
                    updated_at=BASE_TIME,
                )
            )


def _old_analysis(payload: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
    return {
        "job_id": payload["id"],
        "match_score": 55,
        "advantages": json.dumps(["旧优势"], ensure_ascii=False),
        "gaps": json.dumps(["旧差距"], ensure_ascii=False),
        "summary": "旧分析文字",
        "recommendation": "考虑",
        "score_breakdown": {
            "core_direction": 20,
            "required_skills": 15,
            "project_evidence": 10,
            "engineering_stack": 10,
        },
        "evidence": [],
        "evidence_level": "insufficient",
        "matched_directions": ["cpp_software"],
        "primary_match_direction": "cpp_software",
        "analysis_status": "complete",
        "model": model,
        "analysis_version": "matching-v1",
        "prompt_version": "matching-prompt-v1",
        "content_fingerprint": content_fingerprint(payload),
        "profile_fingerprint": profile_fingerprint(PROFILE),
        "filter_reasons": [],
        "refusal_reason": None,
        "error_code": None,
        "input_tokens": None,
        "output_tokens": None,
        "analyzed_at": BASE_TIME,
        "created_at": BASE_TIME,
        "updated_at": BASE_TIME,
        "source": payload["source"],
        "source_ref": f"{payload['source_ref']}:analysis",
    }


def _manifest(
    payloads: list[dict[str, Any]],
    *,
    previous: dict[str, dict[str, Any]] | None = None,
    backup: dict[str, str] | None = None,
) -> dict[str, Any]:
    jobs = []
    for payload in payloads:
        job = dict(payload)
        job["content_fingerprint"] = content_fingerprint(job)
        if previous and payload["id"] in previous:
            job["previous_analysis"] = previous[payload["id"]]
        jobs.append(job)
    return {
        "run_id": "luna-catalog-test",
        "profile": PROFILE,
        "profile_fingerprint": profile_fingerprint(PROFILE),
        "jobs": jobs,
        "backup": backup,
    }


def _analysis(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "matched_directions": ["cpp_software"],
        "primary_match_direction": "cpp_software",
        "score_breakdown": {
            "core_direction": 30,
            "required_skills": 30,
            "project_evidence": 25,
            "engineering_stack": 15,
        },
        "evidence_level": "partial",
        "evidence": [
            {
                "jd_requirement": "C++系统软件开发",
                "profile_evidence": "C++机器人软件项目",
                "relation": "direct",
                "requirement_type": "core",
            },
            {
                "jd_requirement": "Linux 环境开发",
                "profile_evidence": "Linux",
                "relation": "adjacent",
                "requirement_type": "supporting",
            },
        ],
        "missing_core_requirements": [],
        "advantages": ["有项目证据"],
        "gaps": [],
        "summary": "岗位核心要求与已有项目部分匹配。",
    }
    value.update(overrides)
    return value


def _result(job_id: str, *, decision: str = "score", reason: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {
        "job_id": job_id,
        "decision": decision,
        "reason": reason,
    }
    if decision == "score":
        value["analysis"] = _analysis()
    return {"run_id": "luna-catalog-test", "model": "gpt-5.6-luna", "reviews": [value]}


def _backup(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "before-luna.dump"
    path.write_bytes(b"verified backup")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _snapshot(storage: Storage, job_id: str) -> tuple[JobSnapshot, JobAnalysisSnapshot | None]:
    with storage.session() as session:
        return session.get(JobSnapshot, job_id), session.get(JobAnalysisSnapshot, job_id)


def test_score_cap_and_application_are_imported_without_changing_application(
    storage: Storage, tmp_path: Path
) -> None:
    payload = _job_payload()
    _seed_job(storage, payload)
    before = _snapshot(storage, payload["id"])
    manifest = _manifest([payload], backup=_backup(tmp_path))

    report = import_reviewed_scores(
        storage,
        manifest,
        [_result(payload["id"])],
        apply=True,
    )

    assert report["planned"] == 1
    assert report["written"] == 1
    job, analysis = _snapshot(storage, payload["id"])
    assert job.match_score == 89
    assert analysis.match_score == 89
    assert analysis.model == "gpt-5.6-luna"
    assert analysis.source == payload["source"]
    assert before[0].updated_at != job.updated_at
    with storage.session() as session:
        application = session.get(ApplicationSnapshot, f"application-{payload['id']}")
        assert application.stage == "applied"
        assert application.note == "unchanged"
    assert application.updated_at.replace(tzinfo=timezone.utc) == BASE_TIME


@pytest.mark.parametrize(
    ('stored', 'expected'),
    [
        ('["C++ project", "RAG project"]', ['C++ project', 'RAG project']),
        ('legacy text', ['legacy text']),
        ('"legacy JSON string"', ['legacy JSON string']),
        ('', []),
        (None, []),
        ('null', []),
        ('{"invalid": true}', []),
    ],
)
def test_stored_analysis_text_is_not_split_into_characters(stored, expected):
    assert _stored_analysis_strings(stored) == expected


def test_imported_luna_analysis_is_reused_with_the_same_luna_client_without_a_call(
    storage: Storage, tmp_path: Path
) -> None:
    payload = _job_payload()
    _seed_job(storage, payload)
    manifest = _manifest([payload], backup=_backup(tmp_path))
    import_reviewed_scores(storage, manifest, [_result(payload['id'])], apply=True)
    _, stored = _snapshot(storage, payload['id'])

    class NoCallClient:
        model = 'gpt-5.6-luna'

        def complete(self, *args, **kwargs):
            raise AssertionError('unchanged Luna analysis must not call the provider')

    result = MatchingService(NoCallClient()).analyze(
        payload, PROFILE, existing_analysis=_existing_analysis_mapping(stored)
    )
    assert result.decision.action.value == 'reuse'
    assert result.result.model == 'gpt-5.6-luna'
    assert result.result.match_score == stored.match_score
    assert result.result.advantages == json.loads(stored.advantages)
    assert result.result.gaps == json.loads(stored.gaps)


def test_imported_luna_analysis_is_reused_with_a_different_model_using_a_stub(
    storage: Storage, tmp_path: Path
) -> None:
    payload = _job_payload()
    _seed_job(storage, payload)
    manifest = _manifest([payload], backup=_backup(tmp_path))
    import_reviewed_scores(storage, manifest, [_result(payload['id'])], apply=True)
    _, stored = _snapshot(storage, payload['id'])

    class StubClient:
        model = 'deepseek-test'

        def __init__(self):
            self.calls = 0

        def complete(self, **kwargs):
            del kwargs
            self.calls += 1
            return DeepSeekResponse(
                content=json.dumps(_analysis(), ensure_ascii=False),
                model=self.model,
            )

    client = StubClient()
    result = MatchingService(client).analyze(
        payload, PROFILE, existing_analysis=_existing_analysis_mapping(stored)
    )

    assert result.decision.action.value == 'reuse'
    assert result.decision.reason == 'existing_complete_score_reuse'
    assert client.calls == 0
    assert result.result.model == 'gpt-5.6-luna'


def test_old_complete_is_retained_without_whitelist_and_replaced_only_with_frozen_match(
    storage: Storage, tmp_path: Path
) -> None:
    payload = _job_payload()
    old = _old_analysis(payload)
    _seed_job(storage, payload, old_analysis=old)
    manifest = _manifest([payload], previous={payload["id"]: old}, backup=_backup(tmp_path))

    retained = import_reviewed_scores(storage, manifest, [_result(payload["id"])], apply=True)
    assert retained["written"] == 0
    assert retained["reused"] == 1
    assert _snapshot(storage, payload["id"])[1].model is None

    replaced = import_reviewed_scores(
        storage,
        manifest,
        [_result(payload["id"])],
        apply=True,
        replace_complete_job_ids=[payload["id"]],
    )
    assert replaced["written"] == 1
    assert _snapshot(storage, payload["id"])[1].model == "gpt-5.6-luna"


def test_replaced_luna_result_is_idempotent_but_different_result_conflicts(
    storage: Storage, tmp_path: Path
) -> None:
    payload = _job_payload()
    old = _old_analysis(payload)
    _seed_job(storage, payload, old_analysis=old)
    manifest = _manifest([payload], previous={payload["id"]: old}, backup=_backup(tmp_path))
    first = import_reviewed_scores(
        storage,
        manifest,
        [_result(payload["id"])],
        apply=True,
        replace_complete_job_ids=[payload["id"]],
    )
    assert first["written"] == 1
    _, first_analysis = _snapshot(storage, payload["id"])
    first_updated_at = first_analysis.updated_at

    same = import_reviewed_scores(
        storage,
        manifest,
        [_result(payload["id"])],
        apply=True,
        replace_complete_job_ids=[payload["id"]],
    )
    assert same["written"] == 0
    assert same["reused"] == 1
    assert _snapshot(storage, payload["id"])[1].updated_at == first_updated_at

    changed = _result(payload["id"])
    changed["reviews"][0]["analysis"] = _analysis(summary="另一份 Luna 结论")
    conflict = import_reviewed_scores(
        storage,
        manifest,
        [changed],
        apply=True,
        replace_complete_job_ids=[payload["id"]],
    )
    assert conflict["written"] == 0
    assert conflict["rejected"][0]["reason"] == "complete_replace_conflict"
    assert _snapshot(storage, payload["id"])[1].updated_at == first_updated_at


def test_invalid_payload_and_evidence_are_rejected(storage: Storage, tmp_path: Path) -> None:
    payload = _job_payload()
    _seed_job(storage, payload)
    manifest = _manifest([payload], backup=_backup(tmp_path))

    one_evidence = _result(payload["id"])
    one_evidence["reviews"][0]["analysis"]["evidence"] = one_evidence["reviews"][0]["analysis"]["evidence"][:1]
    report = import_reviewed_scores(storage, manifest, [one_evidence])
    assert report["written"] == 0
    assert report["rejected"][0]["reason"] == "evidence_invalid:at_least_two_required"

    invalid = _result(payload["id"])
    invalid["reviews"][0]["analysis"] = _analysis(summary="")
    report = import_reviewed_scores(storage, manifest, [invalid])
    assert report["rejected"][0]["reason"] == "summary_required"

    unverified = _result(payload["id"])
    unverified["reviews"][0]["analysis"] = _analysis(
        evidence=[
            {
                "jd_requirement": "C++系统软件开发",
                "profile_evidence": "SLAM",
                "relation": "direct",
                "requirement_type": "core",
            },
            {
                "jd_requirement": "Linux 环境开发",
                "profile_evidence": "Linux",
                "relation": "adjacent",
                "requirement_type": "supporting",
            },
        ]
    )
    report = import_reviewed_scores(storage, manifest, [unverified])
    assert report["rejected"][0]["reason"].endswith("profile_evidence_not_in_profile")


def test_supporting_skill_cannot_be_core_project_evidence(storage: Storage, tmp_path: Path) -> None:
    payload = _job_payload()
    _seed_job(storage, payload)
    manifest = _manifest([payload], backup=_backup(tmp_path))
    result = _result(payload["id"])
    result["reviews"][0]["analysis"] = _analysis(
        evidence=[
            {
                "jd_requirement": "C++系统软件开发",
                "profile_evidence": "Linux",
                "relation": "direct",
                "requirement_type": "core",
            },
            {
                "jd_requirement": "Linux 环境开发",
                "profile_evidence": "Linux",
                "relation": "adjacent",
                "requirement_type": "supporting",
            },
        ]
    )
    report = import_reviewed_scores(storage, manifest, [result])
    assert report["rejected"][0]["reason"].endswith("supporting_not_core_project")


def test_stale_duplicate_and_cross_batch_results_are_rejected(storage: Storage, tmp_path: Path) -> None:
    payload = _job_payload()
    _seed_job(storage, payload)
    manifest = _manifest([payload], backup=_backup(tmp_path))
    duplicate = import_reviewed_scores(
        storage,
        manifest,
        [_result(payload["id"]), _result(payload["id"])],
    )
    assert duplicate["written"] == 0
    assert {item["reason"] for item in duplicate["rejected"]} == {"duplicate_job_id"}

    wrong_run = _result(payload["id"])
    wrong_run["run_id"] = "other-run"
    report = import_reviewed_scores(storage, manifest, [wrong_run])
    assert report["rejected"][0]["reason"] == "run_id_mismatch"

    with storage.write_transaction() as session:
        session.execute(
            JobSnapshot.__table__.update()
            .where(JobSnapshot.id == payload["id"])
            .values(jd_raw="changed JD")
        )
    stale = import_reviewed_scores(storage, manifest, [_result(payload["id"])])
    assert stale["rejected"][0]["reason"] == "stale_jd_fingerprint"


def test_defer_and_exclude_use_existing_statuses_and_never_zero_score(
    storage: Storage, tmp_path: Path
) -> None:
    defer_job = _job_payload("defer-job")
    exclude_job = _job_payload("exclude-job", title="C++软件开发实习生")
    _seed_job(storage, defer_job, application=False)
    with storage.write_transaction() as session:
        session.add(
            JobSnapshot(
                id=exclude_job["id"],
                company_id=exclude_job["company_id"],
                title=exclude_job["title"],
                city=exclude_job["city"],
                detail_url=exclude_job["detail_url"],
                jd_raw=exclude_job["jd_raw"],
                cohort=exclude_job["cohort"],
                cohort_status=exclude_job["cohort_status"],
                batch=exclude_job["batch"],
                first_seen_at=BASE_TIME,
                last_seen_at=BASE_TIME,
                source=exclude_job["source"],
                source_ref=exclude_job["source_ref"],
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
                capture_evidence=exclude_job["capture_evidence"],
            )
        )
    manifest = _manifest([defer_job, exclude_job], backup=_backup(tmp_path))
    result = {
        "run_id": "luna-catalog-test",
        "model": "gpt-5.6-luna",
        "reviews": [
            {"job_id": defer_job["id"], "decision": "defer", "reason": "JD需要补全"},
            {"job_id": exclude_job["id"], "decision": "exclude", "reason": "实习岗位"},
        ],
    }
    report = import_reviewed_scores(storage, manifest, [result], apply=True)
    assert report["written"] == 2
    defer_row = _snapshot(storage, defer_job["id"])
    exclude_row = _snapshot(storage, exclude_job["id"])
    assert defer_row[0].match_score is None
    assert defer_row[1].analysis_status == "jd_incomplete"
    assert exclude_row[0].match_score is None
    assert exclude_row[1].analysis_status == "internship"


def test_luna_can_exclude_screening_eligible_job_but_score_keeps_hard_screening(
    storage: Storage, tmp_path: Path
) -> None:
    eligible_exclude = _job_payload(
        "eligible-exclude",
        jd_raw=(
            "岗位职责：负责 C++ 系统软件开发和测试，参与交易定价模型实现、"
            "性能优化、问题定位和工程协作，维护线上系统稳定运行。\n"
            "任职要求：熟悉 C++，有 C++机器人软件项目经验，能够在 Linux 环境开发，"
            "具备良好的编程基础、文档阅读能力和团队协作能力。"
        ),
    )
    _seed_job(storage, eligible_exclude, application=False)
    manifest = _manifest([eligible_exclude], backup=_backup(tmp_path))
    excluded = _result(
        eligible_exclude["id"],
        decision="exclude",
        reason="JD明确为交易定价模型岗位，不属于目标方向",
    )

    report = import_reviewed_scores(storage, manifest, [excluded], apply=True)
    assert report["written"] == 1
    job, analysis = _snapshot(storage, eligible_exclude["id"])
    assert job.match_score is None
    assert analysis.analysis_status == "direction_out"
    assert analysis.summary == excluded["reviews"][0]["reason"]
    assert analysis.filter_reasons == [excluded["reviews"][0]["reason"]]
    missing_evidence = _result(
        eligible_exclude["id"],
        decision="exclude",
        reason="不是目标方向",
    )
    rejected = import_reviewed_scores(storage, manifest, [missing_evidence])
    assert rejected["rejected"][0]["reason"] == "exclude_reason_evidence_missing"

    blocked_score = _job_payload("blocked-score", title="C++软件开发实习生")
    with storage.write_transaction() as session:
        session.add(
            JobSnapshot(
                id=blocked_score["id"],
                company_id=blocked_score["company_id"],
                title=blocked_score["title"],
                city=blocked_score["city"],
                detail_url=blocked_score["detail_url"],
                jd_raw=blocked_score["jd_raw"],
                cohort=blocked_score["cohort"],
                cohort_status=blocked_score["cohort_status"],
                batch=blocked_score["batch"],
                first_seen_at=BASE_TIME,
                last_seen_at=BASE_TIME,
                source=blocked_score["source"],
                source_ref=blocked_score["source_ref"],
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
                capture_evidence=blocked_score["capture_evidence"],
            )
        )
    blocked_manifest = _manifest([blocked_score], backup=_backup(tmp_path))
    blocked = import_reviewed_scores(
        storage,
        blocked_manifest,
        [_result(blocked_score["id"])],
    )
    assert blocked["written"] == 0
    assert blocked["rejected"][0]["reason"] == "screening_not_eligible:internship"


def test_score_only_manifest_rejects_exclude_and_does_not_repeat_direction_gates(
    storage: Storage, tmp_path: Path
) -> None:
    payload = _job_payload("score-only", title="C++软件开发实习生")
    _seed_job(storage, payload, application=False)
    manifest = _manifest([payload], backup=_backup(tmp_path))
    manifest["review_mode"] = "score_only"

    excluded = import_reviewed_scores(
        storage,
        manifest,
        [_result(payload["id"], decision="exclude", reason="实习岗位")],
    )
    assert excluded["written"] == 0
    assert excluded["rejected"][0]["reason"] == "decision_not_allowed_in_score_only"

    scored = import_reviewed_scores(
        storage,
        manifest,
        [_result(payload["id"])],
        apply=True,
    )
    assert scored["written"] == 1
    job, analysis = _snapshot(storage, payload["id"])
    assert job.match_score is not None
    assert analysis.analysis_status == "complete"


def test_score_only_manifest_binds_the_selected_execution_model(
    storage: Storage, tmp_path: Path
) -> None:
    payload = _job_payload("deepseek-score-only")
    _seed_job(storage, payload, application=False)
    manifest = _manifest([payload], backup=_backup(tmp_path))
    manifest.update({"review_mode": "score_only", "review_model": "deepseek-v4-flash"})
    result = _result(payload["id"])
    result["model"] = "deepseek-v4-flash"

    report = import_reviewed_scores(storage, manifest, [result], apply=True)

    assert report["written"] == 1
    _, analysis = _snapshot(storage, payload["id"])
    assert analysis.model == "deepseek-v4-flash"


def test_current_profile_fingerprint_is_required_for_apply(storage: Storage, tmp_path: Path) -> None:
    payload = _job_payload()
    _seed_job(storage, payload)
    manifest = _manifest([payload], backup=_backup(tmp_path))
    with pytest.raises(ValueError, match="current_profile_fingerprint_mismatch"):
        import_reviewed_scores(
            storage,
            manifest,
            [_result(payload["id"])],
            apply=True,
            current_profile_fingerprint=profile_fingerprint({"changed": True}),
        )


def test_apply_rolls_back_all_rows_on_unexpected_write_error(
    storage: Storage, tmp_path: Path
) -> None:
    first = _job_payload("job-1")
    second = _job_payload("job-2")
    _seed_job(storage, first, application=False)
    with storage.write_transaction() as session:
        session.add(
            JobSnapshot(
                id=second["id"],
                company_id=second["company_id"],
                title=second["title"],
                city=second["city"],
                detail_url=second["detail_url"],
                jd_raw=second["jd_raw"],
                cohort=second["cohort"],
                cohort_status=second["cohort_status"],
                batch=second["batch"],
                first_seen_at=BASE_TIME,
                last_seen_at=BASE_TIME,
                source=second["source"],
                source_ref=second["source_ref"],
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
                capture_evidence=second["capture_evidence"],
            )
        )
    manifest = _manifest([first, second], backup=_backup(tmp_path))

    def fail_on_second(_mapper: Any, _connection: Any, target: JobAnalysisSnapshot) -> None:
        if target.job_id == second["id"]:
            raise RuntimeError("forced failure")

    event.listen(JobAnalysisSnapshot, "before_insert", fail_on_second)
    try:
        with pytest.raises(ReviewImportTransactionError, match="transaction_rolled_back"):
            import_reviewed_scores(
                storage,
                manifest,
                [_result(first["id"]), _result(second["id"])],
                apply=True,
            )
    finally:
        event.remove(JobAnalysisSnapshot, "before_insert", fail_on_second)

    assert _snapshot(storage, first["id"])[1] is None
    assert _snapshot(storage, second["id"])[1] is None
