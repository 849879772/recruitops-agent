from __future__ import annotations

import asyncio
from types import SimpleNamespace

from packages.browser_bridge.models import OperationStatus
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import ApplicationSnapshot, Storage
from packages.tools import batch_browser_operations as batch_module
from packages.tools.batch_browser_operations import (
    BatchObserveApplicationStatusInput,
    batch_observe_application_status,
)
from packages.tools.typed import ToolStatus


def _repository(tmp_path, applications: list[dict[str, str]]) -> PostgresRecruitmentRepository:
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'batch.db'}", initialize=True)
    with storage.write_transaction() as session:
        for item in applications:
            session.add(ApplicationSnapshot(
                id=item["id"],
                company_name="示例公司",
                job_title=item["title"],
                record_url=item["record_url"],
                stage=item.get("stage", "applied"),
                idempotency_key=f"application:{item['id']}",
                stage_history=[],
                source="test",
                source_ref=item["id"],
            ))
    return PostgresRecruitmentRepository(storage)


def _observed(
    observation: dict[str, object], operation_id: str = "edge-batch-1"
) -> SimpleNamespace:
    return SimpleNamespace(
        success=True,
        error_code=None,
        data=SimpleNamespace(
            operation_id=operation_id,
            error_code=None,
            status=OperationStatus.SUCCEEDED,
            result=observation,
            observation=observation,
        ),
    )


def test_batch_groups_one_page_and_updates_each_bound_application(tmp_path, monkeypatch) -> None:
    record_url = "https://ats.example/applications?session=secret"
    repository = _repository(tmp_path, [
        {"id": "1", "title": "软件开发工程师", "record_url": record_url},
        {"id": "2", "title": "算法工程师", "record_url": record_url, "stage": "written"},
    ])
    requests = []

    async def fake_observe(request, _store, _repository):
        requests.append(request)
        return _observed({
            "page": {"url": record_url, "title": "投递记录", "text": ""},
            "captured_at": "2026-09-04T08:00:00Z",
            "entries": [
                {
                    "status": "written",
                    "label": "笔试中",
                    "context": "示例公司 软件开发工程师 笔试中",
                    "evidence": "软件开发工程师 当前进度：笔试中",
                    "confidence": 0.97,
                },
                {
                    "status": "written",
                    "label": "笔试中",
                    "context": "示例公司 算法工程师 笔试中",
                    "evidence": "算法工程师 当前进度：笔试中",
                    "confidence": 0.97,
                },
            ],
        })

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    response = asyncio.run(
        batch_observe_application_status(
            BatchObserveApplicationStatusInput(application_ids=["1", "2"]),
            object(),  # type: ignore[arg-type]
            repository,
        )
    )

    assert response.status is ToolStatus.SUCCESS
    assert response.success is True
    assert response.read_only is False
    assert response.pages_total == 1
    assert len(requests) == 1
    assert requests[0].application_ids == ["1", "2"]
    assert requests[0].include_vision is False
    assert requests[0].retain_on_pause is False
    assert [item.application_id for item in response.updated] == ["1"]
    assert [item.application_id for item in response.unchanged] == ["2"]
    assert response.summary["write_count"] == 1
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "1").stage == "written"
        assert session.get(ApplicationSnapshot, "2").stage == "written"


def test_batch_reports_missing_evidence_as_unresolved_not_success(tmp_path, monkeypatch) -> None:
    record_url = "https://ats.example/applications"
    repository = _repository(tmp_path, [
        {"id": "1", "title": "软件开发工程师", "record_url": record_url},
    ])

    async def fake_observe(_request, _store, _repository):
        return _observed({
            "page": {"url": record_url, "title": "个人中心", "text": "我的投递"},
            "captured_at": "2026-09-04T08:00:00Z",
            "entries": [],
            "application_records": [],
            "semantic_nodes": [{"text": "我的投递"}],
        })

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    response = asyncio.run(
        batch_observe_application_status(
            BatchObserveApplicationStatusInput(application_ids=["1"], include_vision=False),
            object(),  # type: ignore[arg-type]
            repository,
        )
    )

    assert response.status is ToolStatus.AMBIGUOUS
    assert response.success is False
    assert response.updated == []
    assert response.unchanged == []
    assert response.unresolved[0].reason == "status_evidence_missing"
    assert response.summary["write_count"] == 0
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "1").stage == "applied"


def test_login_shell_is_blocked_but_login_navigation_with_cards_is_not() -> None:
    observation = {"page": {"text": "新华三 首页 校园招聘 登录/注册 投递记录"},
                   "entries": [], "application_records": []}
    assert batch_module._page_authentication_gate(observation)
    observation["application_records"] = [{"title": "软件工程师", "status": "applied"}]
    assert not batch_module._page_authentication_gate(observation)


def test_batch_treats_in_page_identity_verification_as_blocked(tmp_path, monkeypatch) -> None:
    record_url = "https://apply.example/applications"
    repository = _repository(tmp_path, [
        {"id": "1", "title": "算法工程师", "record_url": record_url},
    ])

    async def fake_observe(_request, _store, _repository):
        return _observed({
            "page": {
                "url": record_url,
                "title": "投递查询",
                "text": "请进行身份认证！请填写简历中的手机号码 发送验证码 使用邮箱验证",
            },
            "entries": [],
            "application_records": [],
        })

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    response = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(application_ids=["1"]),
        object(),  # type: ignore[arg-type]
        repository,
    ))

    assert response.blocked[0].reason == "authentication_required"
    assert response.unresolved == []


def test_batch_confirms_existing_applied_when_exact_card_has_no_newer_status(
    tmp_path, monkeypatch
) -> None:
    record_url = "https://ats.example/applications"
    repository = _repository(tmp_path, [
        {"id": "1", "title": "软件工程师（应用软件部）-27届校招(J11510)", "record_url": record_url},
    ])

    async def fake_observe(_request, _store, _repository):
        context = "软件工程师（应用软件部）-27届校招(J11510) 校园招聘 2026-08-20 投递"
        return _observed({
            "page": {"url": record_url, "title": "投递记录", "text": context,
                     "network_requests": [{"url": "https://cdn.example/page_not_found.png"}]},
            "entries": [{"status": "rejected", "context": "另一岗位 流程终止"}],
            "application_records": [{
                "title": "软件工程师（应用软件部）-27届校招(J11510)",
                "status": "",
                "context": context,
                "signals": {"conflicting_statuses": False},
            }],
        })

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    response = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(application_ids=["1"]),
        object(),  # type: ignore[arg-type]
        repository,
    ))

    assert response.unchanged[0].reason == "no_newer_status_observed"
    assert response.unchanged[0].observed_status == "applied"
    assert response.unresolved == []


def test_batch_identifies_removed_application_page_without_changing_stage(
    tmp_path, monkeypatch
) -> None:
    record_url = "https://ats.example/tenant/position/application"
    repository = _repository(tmp_path, [
        {"id": "1", "title": "机器人系统工程师", "record_url": record_url},
    ])

    async def fake_observe(_request, _store, _repository):
        return _observed({
            "page": {
                "url": record_url,
                "title": "应聘记录",
                "text": "首页 职位 社会招聘 1366****246",
                "network_requests": [{
                    "url": "https://cdn.example/saas-career/page_not_found_123.png",
                    "status_code": 200,
                }],
            },
            "entries": [],
            "application_records": [],
        })

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    response = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(application_ids=["1"]),
        object(),  # type: ignore[arg-type]
        repository,
    ))

    assert response.unresolved[0].reason == "status_evidence_missing"
    assert response.unchanged == []
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "1").stage == "applied"


def test_unavailable_requires_visible_error_not_preloaded_illustration():
    observation = {"page": {"text": "投递简历 2026-08-18", "network_requests": [
        {"url": "https://cdn.example/page_not_found.png"}]},
        "application_records": [{"title": "软件工程师", "status": ""}]}
    assert not batch_module._application_page_unavailable(observation)
    observation["page"]["text"] = "页面不存在"
    observation["application_records"] = []
    assert batch_module._application_page_unavailable(observation)
