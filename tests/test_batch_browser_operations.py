from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

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


def test_batch_excludes_closed_applications_before_browser_access(tmp_path, monkeypatch) -> None:
    record_url = "https://ats.example/applications"
    repository = _repository(tmp_path, [
        {"id": "active", "title": "软件开发工程师", "record_url": record_url},
        {"id": "rejected", "title": "算法工程师", "record_url": record_url, "stage": "rejected"},
        {"id": "withdrawn", "title": "测试工程师", "record_url": record_url, "stage": "withdrawn"},
    ])
    requests = []

    async def fake_observe(request, _store, _repository):
        requests.append(request)
        context = "软件开发工程师 投递记录"
        return _observed({
            "page": {"url": record_url, "title": "投递记录", "text": context},
            "entries": [],
            "application_records": [{
                "title": "软件开发工程师",
                "status": "",
                "context": context,
                "signals": {"conflicting_statuses": False},
            }],
        })

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    response = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(
            application_ids=["active", "rejected", "withdrawn"]
        ),
        object(),  # type: ignore[arg-type]
        repository,
    ))

    assert response.success is True
    assert len(requests) == 1
    assert requests[0].application_ids == ["active"]
    assert [item.application_id for item in response.excluded] == ["rejected", "withdrawn"]
    assert all(item.reason == "terminal_stage_excluded" for item in response.excluded)
    assert response.summary["excluded"] == 2
    assert response.pages_total == 1


def test_batch_selects_all_non_terminal_without_application_query(tmp_path, monkeypatch) -> None:
    record_url = "https://ats.example/applications"
    repository = _repository(tmp_path, [
        {"id": "active", "title": "软件开发工程师", "record_url": record_url},
        {"id": "written", "title": "算法工程师", "record_url": record_url, "stage": "written"},
        {"id": "rejected", "title": "测试工程师", "record_url": record_url, "stage": "rejected"},
    ])
    requests = []

    async def fake_observe(request, _store, _repository):
        requests.append(request)
        return _observed({
            "page": {"url": record_url, "title": "投递记录", "text": "投递记录"},
            "entries": [
                {
                    "status": "applied",
                    "label": "已投递",
                    "context": "软件开发工程师 已投递",
                    "evidence": "软件开发工程师 当前进度：已投递",
                    "confidence": 0.97,
                },
                {
                    "status": "written",
                    "label": "笔试中",
                    "context": "算法工程师 笔试中",
                    "evidence": "算法工程师 当前进度：笔试中",
                    "confidence": 0.97,
                },
            ],
            "application_records": [
                {
                    "title": "软件开发工程师",
                    "status": "",
                    "context": "软件开发工程师 投递记录",
                    "signals": {"conflicting_statuses": False},
                },
                {
                    "title": "算法工程师",
                    "status": "written",
                    "label": "笔试中",
                    "context": "算法工程师 笔试中",
                    "signals": {"conflicting_statuses": False},
                },
            ],
        })

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    response = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(all_non_terminal=True),
        object(),  # type: ignore[arg-type]
        repository,
    ))

    assert response.success is True
    assert response.total == 2
    assert response.summary["selection"] == "all_non_terminal"
    assert len(requests) == 1
    assert requests[0].application_ids == ["active", "written"]
    assert not response.excluded


def test_batch_requires_exactly_one_selection_mode() -> None:
    with pytest.raises(ValueError, match="provide application_ids"):
        BatchObserveApplicationStatusInput()
    with pytest.raises(ValueError, match="must be empty"):
        BatchObserveApplicationStatusInput(
            application_ids=["1"],
            all_non_terminal=True,
        )


def test_full_review_missing_bridge_does_not_consume_scope(tmp_path):
    from sqlalchemy import select
    from packages.storage.models import ToolCall

    repository = _repository(tmp_path, [
        {"id": "1", "title": "Engineer", "record_url": "https://ats.example/applications"},
    ])
    result = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(all_non_terminal=True), None, repository,
    ))
    assert not result.success
    with repository.storage.session() as session:
        assert session.scalar(select(ToolCall)) is None


def test_unknown_run_id_does_not_restart_scope(tmp_path):
    from sqlalchemy import select
    from packages.storage.models import ToolCall

    repository = _repository(tmp_path, [])
    result = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(run_id="status-review-" + "a" * 32), object(), repository,
    ))
    assert result.error_code.value == "not_found"
    with repository.storage.session() as session:
        assert session.scalar(select(ToolCall)) is None


@pytest.mark.parametrize("active_count", [39, 61])
def test_full_review_checkpoints_exact_scope_without_reopening_completed_pages(
    tmp_path, monkeypatch, active_count,
) -> None:
    repository = _repository(tmp_path, [
        {"id": str(i), "title": f"Engineer {i}", "record_url": f"https://site{i}.example/applications",
         "stage": "applied" if i < active_count else "rejected"}
        for i in range(active_count + 19)
    ])
    requests = []

    async def fake_observe(request, _store, _repository):
        requests.extend(request.application_ids)
        return _observed({
            "page": {"url": request.application_url, "text": "Applications"},
            "application_records": [{"title": f"Engineer {request.application_id}",
                                     "context": f"Engineer {request.application_id} 投递简历",
                                     "status": "", "signals": {}}],
        })

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)

    async def run():
        result = await batch_observe_application_status(
            BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repository,
        )
        assert result.summary["scope_total"] == active_count
        assert result.summary["excluded_terminal"] == 19
        assert result.summary["processed_count"] == 10
        assert result.summary["scope_complete"] is False
        run_id = result.summary["run_id"]
        # Recreate the repository to verify the cursor is persisted, not in process memory.
        resumed_repository = PostgresRecruitmentRepository(repository.storage)
        if active_count == 39:
            from packages.storage import AgentStateStore
            AgentStateStore(repository.storage).recover_interrupted_task_runs()
            recovered = await batch_observe_application_status(
                BatchObserveApplicationStatusInput(run_id=run_id), object(), resumed_repository,
            )
            assert recovered.summary["run_id"] == run_id
        for _ in range(active_count):
            result = await batch_observe_application_status(
                BatchObserveApplicationStatusInput(run_id=run_id), object(), resumed_repository,
            )
            if result.summary["scope_complete"]:
                break
        assert result.summary["scope_complete"] is True
        assert result.summary["remaining_count"] == 0
        assert result.summary["processed_count"] == active_count
        assert result.summary["unchanged"] == active_count
        assert result.summary["database_total"] == active_count + 19
        # Replaying the completed checkpoint must return the saved result without navigation.
        await batch_observe_application_status(
            BatchObserveApplicationStatusInput(run_id=run_id), object(), resumed_repository,
        )

    asyncio.run(run())
    assert sorted(requests, key=int) == [str(i) for i in range(active_count)]


@pytest.mark.parametrize("shared_origin", [False, True])
def test_full_review_ten_pages_parallel_across_origins_only(tmp_path, monkeypatch, shared_origin):
    repository = _repository(tmp_path, [
        {"id": str(i), "title": f"Engineer {i}",
         "record_url": f"https://{'shared' if shared_origin else f'site{i}'}.example/applications/{i}"}
        for i in range(10)
    ])
    active = peak = 0

    async def fake_observe(request, _store, _repository):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            return _observed({"page": {"url": request.application_url},
                              "application_records": [{"title": f"Engineer {request.application_id}",
                                                       "context": "投递简历", "status": "", "signals": {}}]})
        finally:
            active -= 1

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    result = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repository,
    ))
    assert result.summary["processed_count"] == 10
    assert result.summary["scope_complete"] is True
    assert peak == (1 if shared_origin else 4)


def test_full_review_retains_completed_pages_when_wave_times_out(tmp_path, monkeypatch):
    from packages.tools import application_review_run as run_module

    repository = _repository(tmp_path, [
        {"id": str(i), "title": f"Engineer {i}", "record_url": f"https://site{i}.example/applications"}
        for i in range(2)
    ])
    visits = []

    async def fake_observe(request, _store, _repository):
        visits.append(request.application_id)
        if request.application_id == "1" and visits.count("1") == 1:
            await asyncio.sleep(10)
        return _observed({"page": {"url": request.application_url},
                          "application_records": [{"title": f"Engineer {request.application_id}",
                                                   "status": "", "context": "投递简历", "signals": {}}]})

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    monkeypatch.setattr(run_module, "_WAVE_TIMEOUT_SECONDS", 0.1)

    async def run():
        result = await batch_observe_application_status(
            BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repository,
        )
        assert result.summary["processed_count"] == 2
        assert result.summary["completed_count"] == 1
        assert result.summary["retryable_count"] == 1
        assert result.summary["remaining_count"] == 1
        run_id = result.summary["run_id"]
        # Only an explicit run_id resumes the incomplete scope.
        resumed = await batch_observe_application_status(
            BatchObserveApplicationStatusInput(run_id=run_id), object(), repository,
        )
        assert resumed.summary["run_id"] == run_id
        assert resumed.summary["scope_complete"] is True

    asyncio.run(run())
    assert visits.count("0") == 1
    assert visits.count("1") == 2


def test_full_review_does_not_duplicate_an_in_flight_wave(tmp_path, monkeypatch):
    repository = _repository(tmp_path, [
        {"id": "active", "title": "Engineer", "record_url": "https://site.example/applications"},
    ])
    visits = []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def fake_observe(request, _store, _repository):
            visits.append(request.application_id)
            entered.set()
            await release.wait()
            return _observed({"page": {"url": request.application_url},
                              "application_records": [{"title": "Engineer", "status": "",
                                                       "context": "Engineer 投递简历", "signals": {}}]})

        monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
        first = asyncio.create_task(batch_observe_application_status(
            BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repository,
        ))
        await entered.wait()
        busy = await batch_observe_application_status(
            BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repository,
        )
        assert busy.success is False
        assert "already in progress" in busy.error_message
        release.set()
        finished = await first
        assert finished.summary["scope_complete"] is True

    asyncio.run(run())
    assert visits == ["active"]


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


def test_feishu_tenants_share_one_concurrency_boundary() -> None:
    assert batch_module._origin_concurrency_key(
        "https://alpha.jobs.feishu.cn/1/position/application"
    ) == "ats:feishu"
    assert batch_module._origin_concurrency_key(
        "https://beta.jobs.feishu.cn/2/position/application"
    ) == "ats:feishu"
    assert batch_module._origin_concurrency_key(
        "https://app.mokahr.com/applications"
    ) == "app.mokahr.com"


@pytest.mark.parametrize("error_code", ["CAPTCHA_REQUIRED", "WAITING_FOR_LOGIN", "LOGIN_REQUIRED"])
def test_feishu_auth_pause_is_never_retried(tmp_path, monkeypatch, error_code):
    url = "https://fixture.jobs.feishu.cn/1/position/application"
    repository = _repository(tmp_path, [{"id": "1", "title": "Engineer", "record_url": url}])
    visits = []

    async def observe(request, *_):
        visits.append(request)
        result = _observed({"page": {"url": url, "text": ""}})
        result.data.error_code = error_code
        return result

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", observe)
    result = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(application_ids=["1"]), object(), repository,
    ))
    assert len(visits) == 1
    assert not result.success and len(result.blocked) == 1
    assert not result.failed
    assert not result.updated and not result.unchanged


def test_batch_retries_transient_empty_feishu_shell_once(tmp_path, monkeypatch) -> None:
    record_url = "https://example.jobs.feishu.cn/123/position/application"
    repository = _repository(tmp_path, [
        {"id": "1", "title": "机器人软件工程师", "record_url": record_url},
    ])
    observations = [
        {
            "page": {"url": record_url, "text": "首页 职位 社会招聘 1366****246"},
            "entries": [],
            "application_records": [],
        },
        {
            "page": {"url": record_url, "text": "机器人软件工程师 投递简历 2026-09-18"},
            "entries": [],
            "application_records": [{
                "title": "机器人软件工程师",
                "status": "",
                "context": "机器人软件工程师 投递简历 2026-09-18",
                "signals": {"conflicting_statuses": False},
            }],
        },
    ]
    requests = []

    async def fake_observe(request, _store, _repository):
        requests.append(request)
        return _observed(observations.pop(0), operation_id=f"edge-{len(requests)}")

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    monkeypatch.setattr(batch_module, "_TRANSIENT_RETRY_DELAY_SECONDS", 0)
    response = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(application_ids=["1"]),
        object(),  # type: ignore[arg-type]
        repository,
    ))

    assert len(requests) == 2
    assert requests[0].idempotency_key != requests[1].idempotency_key
    assert response.unchanged[0].reason == "no_newer_status_observed"
    assert response.unresolved == []


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


def test_batch_retains_higher_stage_for_exact_card_with_generic_status(
    tmp_path, monkeypatch
) -> None:
    record_url = "https://ats.example/applications"
    repository = _repository(tmp_path, [
        {"id": "1", "title": "游戏研发-游戏测试开发", "record_url": record_url, "stage": "written"},
    ])

    async def fake_observe(_request, _store, _repository):
        context = "游戏研发-游戏测试开发 状态: 测试中"
        return _observed({
            "page": {"url": record_url, "title": "应聘记录", "text": context},
            "entries": [],
            "application_records": [{
                "title": "游戏研发-游戏测试开发",
                "status": "applied",
                "label": "测试中",
                "raw_status_labels": ["测试中"],
                "context": context,
                "signals": {"unmapped_status": False, "has_explicit_status": True},
            }],
        })

    monkeypatch.setattr(batch_module, "observe_application_status_page_workflow", fake_observe)
    response = asyncio.run(batch_observe_application_status(
        BatchObserveApplicationStatusInput(application_ids=["1"]),
        object(),  # type: ignore[arg-type]
        repository,
    ))

    assert response.success is True
    assert response.unchanged[0].reason == "no_newer_status_observed"
    assert response.unresolved == []
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "1").stage == "written"


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
