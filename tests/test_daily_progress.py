import json

from fastapi.testclient import TestClient

from apps.api import main
from apps.api.daily_progress import latest_daily_progress, task_progress
from packages.scheduler.runtime import _company_checkpoint_progress
from packages.storage import AgentStateStore, Storage, TaskRun


def test_progress_uses_durable_counts_and_survives_interruption(tmp_path):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'progress.db'}", initialize=True)
    store = AgentStateStore(storage)
    store.save_task_state("run-1", {
        "metadata": {"requested_mode": "full", "secret": "not-for-ui"},
        "current_step": "companies:1283/3321",
        "stage_statuses": {"discovery": "succeeded", "crawl": "running"},
        "progress": {"stage": "companies", "scope_total": 3321,
                     "attempted_unique": 1400, "confirmed_complete": 1283,
                     "retry_pending": 117, "checkpoint_ref": "private-path"},
        "progress_updated_at": "2026-09-23T08:00:00+00:00",
    }, ensure_task_run=True)
    with storage.write_transaction() as session:
        session.get(TaskRun, "run-1").status = "running"
    current = latest_daily_progress(storage)["run"]
    assert current["status"] == "running"
    assert current["progress"]["confirmed_complete"] == 1283
    assert current["progress"]["attempted_unique"] == 1400
    assert "secret" not in str(current)
    assert "private-path" not in str(current)

    assert store.recover_interrupted_task_runs() == 1
    recovered = latest_daily_progress(storage)["run"]
    assert recovered["status"] == "stopped"
    assert recovered["progress"]["confirmed_complete"] == 1283


def test_local_ui_progress_requires_same_origin_marker(monkeypatch, tmp_path):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'progress.db'}", initialize=True)
    monkeypatch.setattr(main, "get_storage_engine", lambda: storage.engine)
    client = TestClient(main.app, base_url="http://127.0.0.1:18010")
    url = "/api/local-ui/daily-recruitment/progress"
    assert client.get(url).status_code == 403
    assert client.get(url, headers={"Origin": "https://evil.example", "X-RecruitOps-Local-UI": "1"}).status_code == 403
    result = client.get(url, headers={"Sec-Fetch-Site": "same-origin", "X-RecruitOps-Local-UI": "1"})
    assert result.status_code == 200
    assert result.json() == {"run": None}


def test_company_card_counts_complete_partial_and_failed_outcomes_once(tmp_path):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'processed-progress.db'}", initialize=True)
    store = AgentStateStore(storage)
    checkpoint = tmp_path / "company-checkpoint.json"
    payload = {"company_ids": ["a", "b", "c", "d", "e"], "companies": {
        "a": {"status": "complete", "attempts": 1},
        "b": {"status": "partial", "attempts": 1},
        "c": {"status": "failed", "attempts": 1},
    }}

    def project():
        checkpoint.write_text(json.dumps(payload), encoding="utf-8")
        durable = _company_checkpoint_progress(checkpoint)
        store.save_task_state("crawl-progress", {
            "current_step": "companies:3/5", "progress": {"stage": "companies", **durable},
        }, ensure_task_run=True)
        with storage.write_transaction() as session:
            session.get(TaskRun, "crawl-progress").status = "running"
        return task_progress(storage)["run"]

    result = project()
    assert result["completed"] == 3 and result["total"] == 5
    # Underlying success/retry facts remain available and drive resume unchanged.
    assert result["progress"]["confirmed_complete"] == 1
    assert result["progress"]["retry_pending"] == 2
    assert result["progress"]["remaining"] == 4
    payload["companies"]["c"]["attempts"] = 2
    assert project()["completed"] == 3  # A retried company is never counted twice.
    payload["companies"]["d"] = {"status": "failed", "attempts": 1}
    assert project()["completed"] == 4


def test_company_progress_legacy_fallback_does_not_change_scoring_counts(tmp_path):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'legacy-progress.db'}", initialize=True)
    store = AgentStateStore(storage)
    for stage, progress, expected in [
        ("companies", {"scope_total": 5, "confirmed_complete": 2}, 2),
        ("matching", {"scope_total": 5, "confirmed_complete": 1, "run_attempted": 4}, 1),
    ]:
        store.save_task_state("run-count", {"current_step": stage,
                             "progress": {"stage": stage, **progress}}, ensure_task_run=True)
        store.update_task_progress("run-count", stage)
        with storage.write_transaction() as session:
            session.get(TaskRun, "run-count").status = "running"
        assert task_progress(storage)["run"]["completed"] == expected
