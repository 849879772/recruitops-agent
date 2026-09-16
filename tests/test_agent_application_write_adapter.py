from datetime import datetime, timezone
import json

import pytest

from packages.approval import AgentApplicationWriteAdapter
from packages.storage import ApplicationSnapshot, Storage


def _storage(tmp_path) -> Storage:
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'agent.db'}", initialize=True)
    with storage.write_transaction() as session:
        session.add(
            ApplicationSnapshot(
                id="24",
                company_name="新华三集团",
                job_title="软件开发工程师-C/C++",
                stage="applied",
                idempotency_key="application:24",
                stage_history=[],
                source="legacy_snapshot",
                source_ref="24",
            )
        )
    return storage


def test_agent_adapter_updates_only_agent_application_snapshot(tmp_path) -> None:
    storage = _storage(tmp_path)
    effect = AgentApplicationWriteAdapter(storage).update_application_stage(
        {
            "application_id": 24,
            "current_stage": "applied",
            "target_stage": "written",
            "source_stage": "written",
            "source_status": "笔试中",
            "source_status_synced_at": "2026-08-22T12:00:00Z",
            "result": "进行中",
            "note": "官网状态复核：笔试中",
        }
    )

    with storage.session() as session:
        row = session.get(ApplicationSnapshot, "24")
        assert row is not None
        assert row.stage == "written"
        assert row.source_status == "笔试中"
        # SQLite stores the same wall-clock value without a timezone marker,
        # while PostgreSQL preserves the UTC offset.
        assert row.source_status_synced_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 8, 22, 12, 0, tzinfo=timezone.utc
        )
        assert row.stage_history[-1]["source"] == "edge_application_status_review"
    assert effect.before["stage"] == "applied"
    assert effect.after["stage"] == "written"


def test_agent_adapter_rejects_stage_regression(tmp_path) -> None:
    storage = _storage(tmp_path)
    adapter = AgentApplicationWriteAdapter(storage)
    adapter.update_application_stage(
        {
            "application_id": "24",
            "current_stage": "applied",
            "target_stage": "written",
        }
    )

    with pytest.raises(ValueError, match="move backwards"):
        adapter.update_application_stage(
            {
                "application_id": "24",
                "current_stage": "written",
                "target_stage": "applied",
            }
        )


def test_agent_adapter_atomically_versions_approved_crawler_recipe(tmp_path) -> None:
    storage = _storage(tmp_path)
    recipe_path = tmp_path / "crawler_recipes.json"
    recipe_path.write_text(
        json.dumps({"示例公司": {"type": "html_list", "version": 1}}),
        encoding="utf-8",
    )
    adapter = AgentApplicationWriteAdapter(storage, recipe_path)

    effect = adapter.update_crawler_recipe(
        {
            "company": "示例公司",
            "candidate_id": "a" * 64,
            "recipe": {
                "type": "html_list",
                "listing_url": "https://jobs.example.test/campus",
                "list_selector": ".job",
                "title_selector": ".title",
            },
        }
    )

    saved = json.loads(recipe_path.read_text(encoding="utf-8"))["示例公司"]
    assert saved["version"] == 2
    assert saved["candidate_id"] == "a" * 64
    assert saved["title_selector"] == ".title"
    assert effect.before["recipe"]["version"] == 1
    assert effect.rollback_payload["operation"] == "restore_crawler_recipe"
