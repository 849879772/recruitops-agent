from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from packages.storage import ApplicationSnapshot, Storage, WriteAudit
from packages.tools.browser_status_update import (
    BrowserStatusUpdateInput,
    UpdateStatus,
    browser_status_update,
)


CAPTURED_AT = "2026-08-22T12:00:00Z"


def _storage(tmp_path, applications: list[dict[str, str]]) -> Storage:
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'agent.db'}", initialize=True)
    with storage.write_transaction() as session:
        for item in applications:
            session.add(
                ApplicationSnapshot(
                    id=item["id"],
                    company_name="示例公司",
                    job_title=item["title"],
                    record_url=item.get("record_url", "https://ats.example/applications"),
                    stage=item.get("stage", "applied"),
                    idempotency_key=f"application:{item['id']}",
                    stage_history=[],
                    source="test",
                    source_ref=item["id"],
                )
            )
    return storage


def _request(
    application_id: str,
    *,
    status: str = "written",
    label: str = "笔试中",
    entries: list[dict[str, object]] | None = None,
    page_url: str = "https://ats.example/applications?session=redacted",
) -> BrowserStatusUpdateInput:
    return BrowserStatusUpdateInput(
        application_id=application_id,
        page_url=page_url,
        terminal_result={
            "operation_id": "edge-operation-1",
            "status": status,
            "label": label,
            "entries": entries if entries is not None else [{"status": status, "label": label}],
            "capturedAt": CAPTURED_AT,
        },
    )


def _audit_rows(storage: Storage) -> list[WriteAudit]:
    with storage.session() as session:
        return session.scalars(select(WriteAudit)).all()


def test_id_match_updates_agent_snapshot_with_099_confidence_and_audit(tmp_path) -> None:
    storage = _storage(tmp_path, [{"id": "24", "title": "软件开发工程师"}])

    response = browser_status_update(
        _request(
            "24",
            entries=[
                {
                    "application_id": "24",
                    "status": "written",
                    "label": "笔试中",
                    "context": "示例公司 软件开发工程师 笔试中",
                }
            ],
        ),
        storage,
    )

    assert response.status is UpdateStatus.UPDATED
    assert response.success is True
    assert response.data is not None
    assert response.data.confidence == 0.99
    assert response.data.wrote is True
    with storage.session() as session:
        application = session.get(ApplicationSnapshot, "24")
    assert application is not None
    assert application.stage == "written"
    audits = _audit_rows(storage)
    assert len(audits) == 1
    assert audits[0].success is True
    assert audits[0].evidence_digest


def test_unique_page_match_uses_095_and_replay_is_unchanged_and_idempotent(tmp_path) -> None:
    storage = _storage(tmp_path, [{"id": "24", "title": "软件开发工程师"}])
    request = _request("24")

    first = browser_status_update(request, storage)
    replay = browser_status_update(request, storage)

    assert first.data is not None
    assert first.data.confidence == 0.95
    assert replay.status is UpdateStatus.UNCHANGED
    assert replay.success is True
    assert replay.data is not None
    assert replay.data.idempotent_replay is True
    assert len(_audit_rows(storage)) == 1


def test_unique_title_context_match_uses_090(tmp_path) -> None:
    storage = _storage(
        tmp_path,
        [
            {"id": "24", "title": "软件开发工程师"},
            {"id": "25", "title": "算法工程师"},
        ],
    )

    response = browser_status_update(
        _request(
            "24",
            entries=[
                {
                    "status": "written",
                    "label": "笔试中",
                    "context": "示例公司 软件开发工程师 笔试中",
                },
                {
                    "status": "interview",
                    "label": "面试",
                    "context": "示例公司 算法工程师 面试",
                },
            ],
        ),
        storage,
    )

    assert response.status is UpdateStatus.UPDATED
    assert response.data is not None
    assert response.data.confidence == 0.90
    assert response.data.match_method.value == "unique_title_context"


def test_url_or_ambiguous_evidence_is_state_unclear_without_write(tmp_path) -> None:
    storage = _storage(
        tmp_path,
        [
            {"id": "24", "title": "软件开发工程师"},
            {"id": "25", "title": "算法工程师"},
        ],
    )

    url_mismatch = browser_status_update(
        _request("24", page_url="https://ats.example/other"),
        storage,
    )
    ambiguous = browser_status_update(
        _request(
            "24",
            entries=[
                {"status": "written", "label": "笔试中"},
                {"status": "interview", "label": "面试"},
            ],
        ),
        storage,
    )

    assert url_mismatch.status is UpdateStatus.STATE_UNCLEAR
    assert ambiguous.status is UpdateStatus.STATE_UNCLEAR
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == "applied"
    assert all(not audit.success for audit in _audit_rows(storage))


def test_low_confidence_and_regression_are_blocked_but_rejection_is_synced(tmp_path) -> None:
    storage = _storage(tmp_path, [{"id": "24", "title": "软件开发工程师", "stage": "written"}])

    low_confidence = browser_status_update(
        BrowserStatusUpdateInput(
            application_id="24",
            page_url="https://ats.example/applications",
            terminal_result={
                "status": "interview",
                "label": "面试",
                "entries": [
                    {"application_id": "24", "status": "interview", "label": "面试", "confidence": 0.89}
                ],
                "capturedAt": CAPTURED_AT,
            },
        ),
        storage,
    )
    regressive = browser_status_update(
        _request(
            "24",
            status="applied",
            label="已投递",
            entries=[{"application_id": "24", "status": "applied", "label": "已投递"}],
        ),
        storage,
    )
    rejected = browser_status_update(
        BrowserStatusUpdateInput(
            application_id="24",
            page_url="https://ats.example/applications",
            terminal_result={
                "status": "rejected",
                "label": "已拒绝",
                "entries":[{"application_id": "24", "status": "rejected", "label": "已拒绝"}],
                "capturedAt": CAPTURED_AT,
            },
        ),
        storage,
    )

    assert low_confidence.status is UpdateStatus.STATE_UNCLEAR
    assert low_confidence.data is not None
    assert low_confidence.data.reason_code == "confidence_below_threshold"
    assert low_confidence.data.wrote is False
    assert regressive.status is UpdateStatus.APPROVAL_REQUIRED
    assert rejected.status is UpdateStatus.UPDATED
    assert rejected.data is not None
    assert rejected.data.wrote is True
    with storage.session() as session:
        application = session.get(ApplicationSnapshot, "24")
        assert application.stage == "rejected"
        assert application.stage_history[-1]["result"] == "淘汰"
    assert sum(a.success is True for a in _audit_rows(storage)) == 1


def test_withdrawal_still_requires_approval(tmp_path) -> None:
    storage = _storage(tmp_path, [{"id": "24", "title": "软件开发工程师"}])

    response = browser_status_update(
        _request(
            "24",
            status="withdrawn",
            label="已撤回",
            entries=[{"application_id": "24", "status": "withdrawn", "label": "已撤回"}],
        ),
        storage,
    )

    assert response.status is UpdateStatus.APPROVAL_REQUIRED
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == "applied"


def test_conflicting_status_evidence_is_state_unclear_without_write(tmp_path) -> None:
    storage = _storage(tmp_path, [{"id": "24", "title": "软件开发工程师"}])

    response = browser_status_update(
        BrowserStatusUpdateInput(
            application_id="24",
            page_url="https://ats.example/applications",
            terminal_result={
                "status": "written",
                "label": "笔试中",
                "entries": [
                    {
                        "application_id": "24",
                        "status": "interview",
                        "label": "面试",
                        "context": "示例公司 软件开发工程师",
                    }
                ],
                "capturedAt": CAPTURED_AT,
            },
        ),
        storage,
    )

    assert response.status is UpdateStatus.STATE_UNCLEAR
    assert response.success is False
    assert response.data is not None
    assert response.data.reason_code == "status_evidence_conflict"
    assert response.data.wrote is False
    with storage.session() as session:
        application = session.get(ApplicationSnapshot, "24")
    assert application is not None
    assert application.stage == "applied"
    audits = _audit_rows(storage)
    assert len(audits) == 1
    assert audits[0].success is False
    assert audits[0].error_code == "status_evidence_conflict"


class _FailingAdapter:
    def update_application_stage(self, _payload):
        raise RuntimeError("adapter failure detail must not escape")


def test_adapter_error_is_failure_and_persisted_audit_not_success(tmp_path) -> None:
    storage = _storage(tmp_path, [{"id": "24", "title": "软件开发工程师"}])

    response = browser_status_update(_request("24"), storage, adapter=_FailingAdapter())

    assert response.status is UpdateStatus.FAILED
    assert response.success is False
    assert response.audit_persisted is True
    assert response.error_code == "RuntimeError"
    assert "adapter failure detail" not in response.model_dump_json()
    audits = _audit_rows(storage)
    assert len(audits) == 1
    assert audits[0].success is False
    assert audits[0].error_code == "RuntimeError"
