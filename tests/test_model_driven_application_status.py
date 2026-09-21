from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest

from packages.browser_bridge import BrowserBridgeStore, OperationStatus
from packages.storage import ApplicationSnapshot, Storage
from packages.tools.application_status_evidence import (
    _structured_observation_conflicts,
    VerificationError,
    VerifyApplicationStatusEvidenceInput,
    verify_application_status_evidence,
)


def test_target_submission_is_not_conflicted_by_another_jobs_rejection():
    target = "机器人端到端评测工程师"
    quote = f"{target} 投递简历 2026-08-18"
    result = {
        "status": "rejected",
        "entries": [{"status": "rejected", "context": "机器人软件工程师 流程终止"}],
        "application_records": [
            {"title": target, "context": quote, "status": "", "signals": {}},
            {"title": "机器人软件工程师", "status": "rejected"},
        ],
    }
    args = dict(target_title=target, evidence=quote, observed_label="投递简历")
    assert not _structured_observation_conflicts(result, "19", "applied", **args)
    assert _structured_observation_conflicts(result, "19", "interview", **args)
    result["application_records"][0]["signals"]["conflicting_statuses"] = True
    assert _structured_observation_conflicts(result, "19", "applied", **args)
from packages.tools.browser_bridge import (
    ObserveApplicationStatusPageInput,
    observe_application_status_page,
    observe_application_status_page_workflow,
)


def _prepared_store(tmp_path) -> tuple[BrowserBridgeStore, str]:
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'model-driven-status.db'}", initialize=True)
    page_url = "https://ats.example/applications/24"
    with storage.write_transaction() as session:
        session.add(
            ApplicationSnapshot(
                id="24",
                company_name="中兴通讯",
                job_title="软件开发工程师",
                record_url=page_url,
                stage="applied",
                idempotency_key="application:24",
                stage_history=[],
                source="test",
                source_ref="24",
            )
        )
    return BrowserBridgeStore(storage), page_url


def _observed_operation(
    store: BrowserBridgeStore,
    page_url: str,
    *,
    observation: dict | None = None,
    device_id: str = "edge-1",
):
    created = observe_application_status_page(
        ObserveApplicationStatusPageInput(
            application_id="24",
            application_url=page_url,
            device_id=device_id,
            idempotency_key="observe-24",
        ),
        store,
    )
    assert created.success and created.data is not None
    operation_id = created.data.operation_id
    dispatch = store.fetch_unacked_outbox(device_id)[0]
    store.ack(device_id, dispatch.sequence, operation_id=operation_id)
    store.append_event(operation_id, "validating", OperationStatus.VALIDATING)
    return store.terminal_result(
        operation_id,
        observation
        or {
            "application_id": "24",
            "application_ids": ["24"],
            "page_url": page_url,
            "captured_at": "2026-08-22T12:00:00Z",
            "page": {"title": "投递记录", "text": "软件开发工程师 投递成功 综合测评"},
            "semantic_nodes": [
                {
                    "tag": "li",
                    "text": "投递成功",
                    "attributes": {"aria-current": "step"},
                    "visual": {"color": "rgb(0, 102, 255)"},
                }
            ],
        },
        status=OperationStatus.SUCCEEDED,
        event_id="observed",
    )


@pytest.mark.parametrize("fault", [None, "missing_binding", "foreign_origin", "wrong_request", "wrong_capture", "old_record_url", "non_desktop"])
def test_owned_navigation_keeps_actual_capture_and_binds_original_application(tmp_path, fault):
    store, target = _prepared_store(tmp_path)
    observed = "https://ats.example/applications/24#/app/application_center"
    if fault == "foreign_origin":
        observed = "https://foreign.example/applications/24"
    captured = datetime.now(timezone.utc)
    observation = {
        "application_ids": ["24"], "page_url": observed, "captured_at": captured.isoformat(),
        "page": {"page_url": observed, "text": "软件开发工程师 笔试中"},
        "entries": [{"application_id": "24", "status": "written", "label": "笔试中", "context": "软件开发工程师 笔试中", "confidence": 0.99}],
        "navigation_binding": {"source": "desktop_owned_navigation_v1", "requested_page_url": target, "observed_page_url": observed},
    }
    if fault == "missing_binding":
        observation.pop("navigation_binding")
    if fault == "wrong_request":
        observation["navigation_binding"]["requested_page_url"] = "https://ats.example/another-application"
    if fault == "wrong_capture":
        observation["page"]["page_url"] = target
    operation = _observed_operation(store, target, observation=observation,
                                    device_id="edge-1" if fault == "non_desktop" else "desktop-fixture")
    if fault == "old_record_url":
        with store.storage.write_transaction() as session:
            session.get(ApplicationSnapshot, "24").record_url = "https://ats.example/new-record-url"
    result = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
        application_id="24", observation_operation_id=operation.operation_id,
        observed_status="written", observed_label="笔试中", evidence="软件开发工程师 笔试中",
        confidence=0.99, captured_at=captured,
    ), store)
    with store.storage.session() as session:
        row = session.get(ApplicationSnapshot, "24")
        if fault:
            assert not result.success
            assert result.error_code == VerificationError.OBSERVATION_BINDING_MISMATCH
            assert row.stage == "applied"
        else:
            assert result.success, result
            assert row.stage == "written"
            assert row.record_url == target
    assert store.get_operation(operation.operation_id).result["page_url"] == observed


def test_model_evidence_can_verify_a_high_confidence_unchanged_status(tmp_path) -> None:
    store, page_url = _prepared_store(tmp_path)
    operation = _observed_operation(store, page_url)

    result = verify_application_status_evidence(
        VerifyApplicationStatusEvidenceInput(
            application_id="24",
            observation_operation_id=operation.operation_id,
            observed_status="applied",
            observed_label="投递成功",
            evidence="投递成功",
            confidence=0.96,
            captured_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        ),
        store,
    )

    assert result.success is True
    assert result.status == "unchanged"
    assert result.verification is not None
    assert result.verification.data is not None
    assert result.verification.data.reason_code == "unchanged"


def test_generic_testing_label_overrides_model_written_guess_and_retains_history(tmp_path) -> None:
    store, page_url = _prepared_store(tmp_path)
    with store.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "24").stage = "written"
    evidence = "软件开发工程师 测试中"
    operation = _observed_operation(store, page_url, observation={
        "page_url": page_url,
        "captured_at": "2026-08-22T12:00:00Z",
        "page": {"title": "应聘记录", "text": evidence},
        "semantic_nodes": [{"tag": "td", "text": "测试中"}],
        "entries": [],
        "application_records": [],
    })

    result = verify_application_status_evidence(
        VerifyApplicationStatusEvidenceInput(
            application_id="24",
            observation_operation_id=operation.operation_id,
            observed_status="written",
            observed_label="测试中",
            evidence=evidence,
            confidence=0.85,
            captured_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        ),
        store,
    )

    assert result.success is True
    assert result.status == "unchanged"
    assert result.verification is not None and result.verification.data is not None
    assert result.verification.data.reason_code == "historical_stage_retained"
    assert result.verification.data.current_stage.value == "written"
    assert result.verification.data.target_stage.value == "written"


def test_exact_submission_card_is_read_only_unchanged_despite_low_model_confidence(tmp_path):
    store, page_url = _prepared_store(tmp_path)
    quote = "软件开发工程师 投递简历 2026-08-22"
    operation = _observed_operation(store, page_url, observation={
        "page_url": page_url, "captured_at": "2026-08-22T12:00:00Z", "entries": [],
        "application_records": [{"title": "软件开发工程师", "status": "", "context": quote}],
    })
    request = VerifyApplicationStatusEvidenceInput(
        application_id="24", observation_operation_id=operation.operation_id,
        observed_status="applied", observed_label="投递简历", evidence=quote,
        confidence=0.75, captured_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )
    result = verify_application_status_evidence(request, store)
    assert result.success and result.status == "unchanged" and result.read_only
    assert result.reason_code == "no_newer_status_observed"
    with store.storage.session() as session:
        app = session.get(ApplicationSnapshot, "24")
        assert app.stage == "applied" and app.stage_history == []
    with store.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "24").stage = "rejected"
    result = verify_application_status_evidence(request, store)
    assert not result.success
    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == "rejected"


def test_model_cannot_commit_evidence_that_was_not_observed(tmp_path) -> None:
    store, page_url = _prepared_store(tmp_path)
    operation = _observed_operation(store, page_url)

    result = verify_application_status_evidence(
        VerifyApplicationStatusEvidenceInput(
            application_id="24",
            observation_operation_id=operation.operation_id,
            observed_status="offer",
            observed_label="已录用",
            evidence="已录用",
            confidence=0.99,
            captured_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        ),
        store,
    )

    assert result.success is False
    assert result.error_code is VerificationError.EVIDENCE_NOT_IN_OBSERVATION


def test_model_cannot_commit_an_unobserved_status_label(tmp_path) -> None:
    store, page_url = _prepared_store(tmp_path)
    operation = _observed_operation(store, page_url)

    result = verify_application_status_evidence(
        VerifyApplicationStatusEvidenceInput(
            application_id="24",
            observation_operation_id=operation.operation_id,
            observed_status="offer",
            observed_label="已录用",
            evidence="投递成功",
            confidence=0.99,
            captured_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        ),
        store,
    )

    assert result.success is False
    assert result.error_code is VerificationError.EVIDENCE_NOT_IN_OBSERVATION


def test_model_cannot_override_conflicting_structured_observation(tmp_path) -> None:
    store, page_url = _prepared_store(tmp_path)
    operation = _observed_operation(
        store,
        page_url,
        observation={
            "application_id": "24",
            "application_ids": ["24"],
            "page_url": page_url,
            "captured_at": "2026-08-22T12:00:00Z",
            "status": "written",
            "label": "笔试中",
            "entries": [
                {
                    "application_id": "24",
                    "status": "interview",
                    "label": "面试",
                    "context": "软件开发工程师",
                }
            ],
        },
    )

    result = verify_application_status_evidence(
        VerifyApplicationStatusEvidenceInput(
            application_id="24",
            observation_operation_id=operation.operation_id,
            observed_status="written",
            observed_label="笔试中",
            evidence="笔试中",
            confidence=0.99,
            captured_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        ),
        store,
    )

    assert result.success is False
    assert result.status == "STATE_UNCLEAR"
    assert result.error_code is VerificationError.STATUS_EVIDENCE_CONFLICT
    with store.storage.session() as session:
        application = session.get(ApplicationSnapshot, "24")
    assert application is not None
    assert application.stage == "applied"


def test_model_binds_named_record_and_auto_syncs_rejection_on_multi_record_page(tmp_path) -> None:
    store, page_url = _prepared_store(tmp_path)
    target_evidence = "软件开发工程师 当前进度：用人部门筛选-淘汰"
    operation = _observed_operation(
        store,
        page_url,
        observation={
            "application_id": "24",
            "application_ids": ["24"],
            "page_url": page_url,
            "captured_at": "2026-09-04T10:42:54Z",
            "entries": [
                {
                    "status": "rejected",
                    "label": "用人部门筛选-淘汰",
                    "context": target_evidence,
                    "evidence": target_evidence,
                    "confidence": 0.97,
                },
                {
                    "status": "rejected",
                    "label": "简历初筛-淘汰",
                    "context": "算法工程师 当前进度：简历初筛-淘汰",
                    "evidence": "算法工程师 当前进度：简历初筛-淘汰",
                    "confidence": 0.97,
                },
            ],
        },
    )

    result = verify_application_status_evidence(
        VerifyApplicationStatusEvidenceInput(
            application_id="24",
            observation_operation_id=operation.operation_id,
            observed_status="rejected",
            observed_label="用人部门筛选-淘汰",
            evidence=target_evidence,
            confidence=0.97,
            captured_at=datetime(2026, 9, 4, 10, 42, 54, tzinfo=timezone.utc),
        ),
        store,
    )

    assert result.success is True
    assert result.status == "updated"
    with store.storage.session() as session:
        application = session.get(ApplicationSnapshot, "24")
    assert application is not None
    assert application.stage == "rejected"
    assert application.stage_history[-1]["result"] == "淘汰"


def test_observation_workflow_returns_a_typed_timeout_response(tmp_path) -> None:
    store, page_url = _prepared_store(tmp_path)
    repository = SimpleNamespace(
        list_applications=lambda: [SimpleNamespace(id="24", record_url=page_url)]
    )

    result = asyncio.run(
        observe_application_status_page_workflow(
            ObserveApplicationStatusPageInput(
                application_id="24",
                device_id="edge-timeout",
                idempotency_key="observe-timeout-24",
                timeout_ms=1,
            ),
            store,
            repository,
        )
    )

    assert result.success is False
    assert result.error_code is not None and result.error_code.value == "timeout"
    assert result.timed_out is True
    operation = store.get_by_idempotency_key("observe-timeout-24")
    assert operation is not None
    assert operation.status == OperationStatus.CANCELLED


def test_observation_workflow_refuses_closed_application_before_browser_access(tmp_path) -> None:
    store, page_url = _prepared_store(tmp_path)
    repository = SimpleNamespace(
        list_applications=lambda: [
            SimpleNamespace(id="24", record_url=page_url, stage="rejected")
        ]
    )

    result = asyncio.run(
        observe_application_status_page_workflow(
            ObserveApplicationStatusPageInput(
                application_id="24",
                device_id="edge-closed",
                idempotency_key="observe-closed-24",
            ),
            store,
            repository,
        )
    )

    assert result.success is False
    assert result.error_code is not None and result.error_code.value == "invalid_input"
    assert "closed" in str(result.error_message).lower()
    assert store.get_by_idempotency_key("observe-closed-24") is None


def test_observation_default_timeout_covers_spa_and_vision_latency() -> None:
    request = ObserveApplicationStatusPageInput(
        application_id="24",
        device_id="edge-1",
        idempotency_key="observe-default-timeout-24",
    )

    assert request.timeout_ms == 45_000
