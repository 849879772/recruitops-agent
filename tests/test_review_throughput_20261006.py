"""Deterministic admission/checkpoint tests; no browser, provider or formal DB."""
import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.storage.models import ApplicationSnapshot, TaskRun, ToolCall
from packages.tools import application_review_run as review
from tests.test_application_review_run import _part, _repository, _review_input


def _success(request):
    return _part(request, {item: ("unchanged", None) for item in request.application_ids})


def _ordered(repository, monkeypatch):
    original = repository.list_applications
    monkeypatch.setattr(repository, "list_applications", lambda: sorted(original(), key=lambda row: int(row.id)))


@pytest.mark.parametrize("before,after", [(6, 4), (4, 2), (2, 2), (1, 1)])
def test_pressure_admission_only_moves_down_to_a_bounded_floor(before, after):
    assert review._lower_concurrency(before) == after


@pytest.mark.parametrize("reason", ["target_record_ambiguous", "login_required", "model_timeout", "model_unavailable"])
def test_unrelated_unresolved_outcomes_do_not_become_automatic_retries(reason):
    assert not review._is_transient_failure({"state": "unresolved", "reason": reason})
    assert not review._is_transient_failure({"state": "unresolved", "reason": reason,
                                             "vision_disposition": "transport_failed"})


@pytest.mark.parametrize("code", ["vision_queue_timeout", "vision_duplicate_wait_timeout"])
@pytest.mark.parametrize("provider_count", [1, None, "0", False])
def test_queue_failures_without_explicit_zero_paid_requests_never_retry(code, provider_count):
    diagnostics = {} if provider_count is None else {"vision_provider_request_count": provider_count}
    assert not review._is_transient_failure({"state": "unresolved", "reason": "status_evidence_missing",
        "vision_disposition": code, "diagnostics": diagnostics})
    assert not review._is_transient_failure({"state": "unresolved", "reason": "status_evidence_missing",
        "diagnostics": {**diagnostics, "vision_error": code}})


@pytest.mark.parametrize("code,diagnostic_only", [("vision_queue_timeout", False),
    ("vision_duplicate_wait_timeout", False), ("vision_queue_timeout", True)])
@pytest.mark.parametrize("recovers", [True, False])
def test_local_vision_queue_retries_preserve_unresolved_and_stop_after_three_attempts(
        tmp_path, monkeypatch, code, diagnostic_only, recovers):
    repository = _repository(tmp_path, 1)
    visits = []

    async def observe(request, *_):
        visits.append(list(request.application_ids))
        if recovers and len(visits) == 2:
            return _success(request)
        part = _part(request, {"0": ("unresolved", "status_evidence_missing")})
        part.unresolved[0].diagnostics = {"vision_provider_request_count": 0}
        if diagnostic_only:
            part.unresolved[0].diagnostics["vision_error"] = code
        else:
            part.unresolved[0].vision_disposition = code
        return part

    monkeypatch.setattr(review, "batch_observe_application_status", observe)

    async def run():
        result = await review.continue_application_review(_review_input(), object(), repository)
        run_id = result.summary["run_id"]
        assert result.summary["retryable_count"] == 1 and result.summary["remaining_count"] == 1
        assert result.unresolved[0].reason == "status_evidence_missing" and not result.failed
        while result.summary["continuation_required"]:
            result = await review.continue_application_review(_review_input(run_id), object(), repository)
        assert result.summary["scope_complete"] and result.summary["remaining_count"] == 0
        assert len(visits) == (2 if recovers else 3)
        assert result.summary["retry_exhausted_count"] == (0 if recovers else 1)
        assert bool(result.unchanged) is recovers and not result.failed
        with repository.storage.session() as session:
            state = session.get(ToolCall, run_id).arguments
            assert state["attempts"]["0"] == len(visits)
            assert state["pressure_concurrency_limit"] == (4 if recovers else 2)
        await review.continue_application_review(_review_input(run_id), object(), repository)
        assert len(visits) == (2 if recovers else 3)

    asyncio.run(run())


@pytest.mark.parametrize("queue_code", ["vision_queue_timeout", "vision_duplicate_wait_timeout"])
def test_queue_retry_uses_fresh_dom_and_screenshot_keys_in_the_same_run(tmp_path, monkeypatch, queue_code):
    from packages import config
    from packages.tools import application_status_model as model
    from packages.tools import batch_browser_operations as batch
    from tests.test_batch_browser_operations import _observed

    repository = _repository(tmp_path, 1)
    observation = {"page": {"url": "https://site0.example/applications", "text": "Example Engineer 0"},
                   "application_records": [], "semantic_nodes": [], "entries": [], "diagnostics": {}}
    calls = []
    operations = []

    async def observe(request, *_):
        calls.append(request)
        operation_id = f"fixture-operation-{len(calls)}"
        operations.append(operation_id)
        if not request.include_vision:
            return _observed(observation, operation_id)
        extra = ({"vision_error": queue_code} if len(calls) == 2 else
                 {"vision": {"text": "Example Engineer 0 Current status: Applied"}})
        return _observed({**observation, **extra}, operation_id)

    async def visual_review(_store, _operation, _applications, targets):
        return {key: {"state": "unchanged", "reason": "same_stage", "wrote": False,
                      "model_disposition": "model_called"} for key in targets}

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(write_enabled=True, llm_enabled=True,
        llm_api_key="fixture", vision_enabled=True, vision_model="deepseek-flash"))
    monkeypatch.setattr(batch, "observe_application_status_page_workflow", observe)
    monkeypatch.setattr(model, "resolve_visual_statuses", visual_review)
    store = SimpleNamespace(get_events=lambda _: [])

    async def run():
        initial = await review.continue_application_review(_review_input(), store, repository)
        run_id = initial.summary["run_id"]
        assert initial.unresolved[0].vision_disposition == queue_code
        assert initial.summary["continuation_required"]
        resumed = await review.continue_application_review(_review_input(run_id), store, repository)
        assert resumed.summary["run_id"] == run_id and resumed.summary["scope_complete"]
        assert len(resumed.unchanged) == 1
        assert len(calls) == 4 and len({call.idempotency_key for call in calls}) == 4
        assert [call.include_vision for call in calls] == [False, True, False, True]
        assert calls[1].reuse_observation_operation_id == operations[0]
        assert calls[3].reuse_observation_operation_id == operations[2]
        assert calls[1].idempotency_key == f"batch-vision-{operations[0]}"
        assert calls[3].idempotency_key == f"batch-vision-{operations[2]}"
        with repository.storage.session() as session:
            state = session.get(ToolCall, run_id).arguments
            assert state["attempts"]["0"] == 2

    asyncio.run(run())


def test_window_refills_past_ten_pages_without_waiting_for_a_slow_sibling(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 24)
    _ordered(repository, monkeypatch)

    async def run():
        release = asyncio.Event()
        visited = []
        active = peak = 0

        async def observe(request, *_):
            nonlocal active, peak
            assert request.max_concurrent == 1
            active += 1
            peak = max(peak, active)
            visited.extend(request.application_ids)
            try:
                if request.application_ids == ["0"]:
                    await release.wait()
                else:
                    await asyncio.sleep(0)
                    if len(visited) >= 20:
                        release.set()
                return _success(request)
            finally:
                active -= 1

        monkeypatch.setattr(review, "batch_observe_application_status", observe)
        result = await asyncio.wait_for(review.continue_application_review(_review_input(), object(), repository), 5)
        assert result.summary["scope_complete"] and result.summary["completed_count"] == 24
        assert peak == 6 and active == 0
        assert len(visited) == len(set(visited)) == 24

    asyncio.run(run())


def test_busy_origin_never_occupies_slots_needed_by_independent_sites(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 12)
    with repository.storage.write_transaction() as session:
        for index in range(9):
            session.get(ApplicationSnapshot, str(index)).record_url = f"https://app.mokahr.com/company{index}"
    _ordered(repository, monkeypatch)

    async def run():
        independent_started = set()
        all_independent = asyncio.Event()
        active = Counter()

        async def observe(request, *_):
            identifier = int(request.application_ids[0])
            origin = "moka" if identifier < 9 else str(identifier)
            active[origin] += 1
            assert active[origin] == 1
            try:
                if identifier < 9:
                    await all_independent.wait()
                else:
                    independent_started.add(identifier)
                    if independent_started == {9, 10, 11}:
                        all_independent.set()
                await asyncio.sleep(0)
                return _success(request)
            finally:
                active[origin] -= 1

        monkeypatch.setattr(review, "batch_observe_application_status", observe)
        result = await asyncio.wait_for(review.continue_application_review(_review_input(), object(), repository), 5)
        assert result.summary["completed_count"] == 12 and not any(active.values())

    asyncio.run(run())


@pytest.mark.parametrize("task_type,status,expected", [
    ("daily_recruitment_intelligence", "running", 4), ("daily_recruitment_sync", "running", 4),
    ("daily_recruitment_intelligence", "pausing", 4), ("daily_recruitment_sync", "completed", 6),
])
def test_active_daily_task_conservatively_reduces_review_load(tmp_path, monkeypatch, task_type, status, expected):
    repository = _repository(tmp_path, 12)
    with repository.storage.write_transaction() as session:
        session.add(TaskRun(id="fixture-crawler", task_type=task_type, status=status,
                            user_request="Anonymous crawler fixture", source="test"))
    active = peak = 0

    async def observe(request, *_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
            return _success(request)
        finally:
            active -= 1

    monkeypatch.setattr(review, "batch_observe_application_status", observe)
    result = asyncio.run(review.continue_application_review(_review_input(), object(), repository))
    assert peak == expected and active == 0 and result.summary["scope_complete"]


@pytest.mark.parametrize("channel,code", [("reason", "browser_busy"), ("reason", "http_429"),
    ("vision_disposition", "vision_queue_timeout"), ("vision_disposition", "vision_duplicate_wait_timeout"),
    ("diagnostics", "vision_queue_timeout")])
def test_queue_or_provider_pressure_reduces_admission_without_losing_receipts(tmp_path, monkeypatch, channel, code):
    repository = _repository(tmp_path, 18)
    _ordered(repository, monkeypatch)

    async def run():
        first_six = asyncio.Event()
        active = entered = later_peak = 0

        async def observe(request, *_):
            nonlocal active, entered, later_peak
            identifier = int(request.application_ids[0])
            active += 1
            entered += 1
            if entered == 6:
                first_six.set()
            try:
                if identifier < 6:
                    await first_six.wait()
                    part = _part(request, {str(identifier): ("failed", code if channel == "reason" else "vision_timeout")})
                    if channel == "vision_disposition":
                        part.failed[0].reason = "status_evidence_missing"
                        part.failed[0].vision_disposition = code
                    elif channel == "diagnostics":
                        part.failed[0].reason = "status_evidence_missing"
                        part.failed[0].diagnostics = {"vision_error": code}
                    return part
                later_peak = max(later_peak, active)
                await asyncio.sleep(0.005)
                return _success(request)
            finally:
                active -= 1

        monkeypatch.setattr(review, "batch_observe_application_status", observe)
        result = await review.continue_application_review(_review_input(), object(), repository)
        assert entered == 18 and active == 0 and later_peak == 2
        with repository.storage.session() as session:
            state = session.get(ToolCall, result.summary["run_id"]).arguments
            assert state["pressure_concurrency_limit"] == 2
            assert len(state["results"]) == 18 and set(state["attempts"].values()) == {1}

    asyncio.run(run())


def test_timeout_drains_only_dispatched_pages_and_resume_never_repeats_completed(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 20)
    _ordered(repository, monkeypatch)
    monkeypatch.setattr(review, "_WAVE_TIMEOUT_SECONDS", .15)
    monkeypatch.setattr(review, "_MIN_PAGE_START_SECONDS", .01)

    async def run():
        completed, entered, cleaned = set(), set(), set()

        async def observe(request, *_):
            identifier = request.application_ids[0]
            entered.add(identifier)
            try:
                if identifier == "0":
                    completed.add(identifier)
                    return _success(request)
                await asyncio.Event().wait()
            finally:
                cleaned.add(identifier)

        monkeypatch.setattr(review, "batch_observe_application_status", observe)
        first = await review.continue_application_review(_review_input(), object(), repository)
        assert first.summary["wave_error"] == "review_wave_timeout"
        assert completed == {"0"} and entered == cleaned and len(entered) < 20
        with repository.storage.session() as session:
            state = session.get(ToolCall, first.summary["run_id"]).arguments
            assert set(state["results"]) == entered and set(state["attempts"]) == entered
            assert state["lease_until"] == 0
        visited = []

        async def recovered(request, *_):
            visited.extend(request.application_ids)
            return _success(request)

        monkeypatch.setattr(review, "batch_observe_application_status", recovered)
        monkeypatch.setattr(review, "_WAVE_TIMEOUT_SECONDS", 10)
        second = await review.continue_application_review(_review_input(first.summary["run_id"]), object(), repository)
        assert second.summary["scope_complete"] and set(visited) == {str(i) for i in range(1, 20)}
        assert len(visited) == 19

    asyncio.run(run())


def test_page_safety_cap_is_admission_only_and_resume_preserves_counts(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 17)
    monkeypatch.setattr(review, "_WAVE_PAGES", 11)
    visited = []

    async def observe(request, *_):
        visited.extend(request.application_ids)
        return _success(request)

    monkeypatch.setattr(review, "batch_observe_application_status", observe)
    first = asyncio.run(review.continue_application_review(_review_input(), object(), repository))
    assert first.summary["completed_count"] == 11 and first.summary["remaining_count"] == 6
    second = asyncio.run(review.continue_application_review(_review_input(first.summary["run_id"]), object(), repository))
    assert second.summary["scope_complete"] and second.summary["completed_count"] == 17
    assert len(visited) == len(set(visited)) == 17


def test_late_window_reserves_startup_time_without_failing_unstarted_pages(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 20)
    clock = [0.0]
    monkeypatch.setattr(review, "perf_counter", lambda: clock[0])
    visited = []

    async def observe(request, *_):
        visited.extend(request.application_ids)
        clock[0] = 91.0  # 14 seconds remain in the production 105-second window.
        return _success(request)

    monkeypatch.setattr(review, "batch_observe_application_status", observe)
    result = asyncio.run(review.continue_application_review(_review_input(), object(), repository))
    assert len(visited) == 6 and result.summary["remaining_count"] == 14
    assert result.summary["continuation_required"] and result.summary["wave_error"] is None
    with repository.storage.session() as session:
        state = session.get(ToolCall, result.summary["run_id"]).arguments
        assert set(state["attempts"]) == set(visited) and len(state["results"]) == 6


def test_large_same_page_scope_chunks_stay_serial_and_resume_only_unfinished_ids(tmp_path, monkeypatch):
    repository = _repository(tmp_path, 103)
    with repository.storage.write_transaction() as session:
        for app in session.scalars(select(ApplicationSnapshot)):
            app.record_url = "https://same.example/applications"
    monkeypatch.setattr(review, "_WAVE_PAGES", 1)
    visited, sizes = [], []

    async def observe(request, *_):
        visited.extend(request.application_ids)
        sizes.append(len(request.application_ids))
        return _success(request)

    monkeypatch.setattr(review, "batch_observe_application_status", observe)
    result = asyncio.run(review.continue_application_review(_review_input(), object(), repository))
    assert result.summary["completed_count"] == 50
    while result.summary["continuation_required"]:
        result = asyncio.run(review.continue_application_review(_review_input(result.summary["run_id"]), object(), repository))
    assert sizes == [50, 50, 3] and len(visited) == len(set(visited)) == 103
