import json
from pathlib import Path

import pytest

from packages.approval import AutumnSystemWriteAdapter, SourceBackupManager


def _source_root(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "config.yaml").write_text(
        "profile:\n  degree: 研究生\ncompanies:\n- name: 已有公司\n  careers_url: https://example.com/jobs\n  crawler: render\ndeepseek:\n  model: flash\n",
        encoding="utf-8",
    )
    (tmp_path / "data" / "applications.json").write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "company": "示例公司",
                    "title": "C++工程师",
                    "current_stage": "applied",
                    "stages": [],
                    "events": [],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_company_update_preserves_other_config_sections(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    adapter = AutumnSystemWriteAdapter(root)

    effect = adapter.update_company_config(
        {
            "company": {
                "name": "新公司",
                "careers_url": "https://careers.example.com/campus",
                "crawler": "render",
                "aliases": ["新公司科技"],
            }
        }
    )

    content = (root / "config.yaml").read_text(encoding="utf-8")
    assert "deepseek:" in content
    assert "新公司" in content
    assert effect.rollback_payload == {"operation": "remove_company", "name": "新公司"}


def test_application_and_schedule_writes_are_exact_and_reject_regression(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    adapter = AutumnSystemWriteAdapter(root)

    stage = adapter.update_application_stage(
        {
            "application_id": 1,
            "current_stage": "applied",
            "target_stage": "written",
            "result": "进行中",
        }
    )
    assert stage.before["current_stage"] == "applied"
    assert stage.after["current_stage"] == "written"

    synced = adapter.update_application_stage(
        {
            "application_id": 1,
            "current_stage": "written",
            "target_stage": "interview1",
            "source_stage": "interview",
            "source_status": "面试",
            "source_status_synced_at": "2026-08-19T12:00:00+00:00",
        }
    )
    assert synced.after["source_stage"] == "interview"
    assert synced.after["source_status"] == "面试"
    assert synced.after["source_status_synced_at"].startswith("2026-08-19")

    schedule = adapter.create_schedule(
        {
            "application_id": 1,
            "event_type": "笔试",
            "event_date": "2026-08-21",
            "event_time": "19:30",
            "note": "线上",
        }
    )
    assert schedule.after["event"]["event_date"] == "2026-08-21"

    with pytest.raises(ValueError, match="backwards"):
        adapter.update_application_stage(
                {
                    "application_id": 1,
                    "current_stage": "interview1",
                    "target_stage": "applied",
                }
        )


def test_source_backup_copies_mutable_files(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    manager = SourceBackupManager(root, tmp_path / "backup")

    manager()

    assert manager.last_backup is not None
    assert (manager.last_backup / "config.yaml").is_file()
    assert (manager.last_backup / "data" / "applications.json").is_file()


def test_application_capture_write_is_idempotent_and_preserves_record_url(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    adapter = AutumnSystemWriteAdapter(root)
    payload = {
        "job_id": "job-27",
        "company": "示例公司",
        "title": "机器人软件工程师",
        "city": "上海",
        "record_url": "https://example.com/jobs/27",
        "source_job_url": "https://example.com/jobs/27",
        "captured_at": "2026-08-20T10:00:00+08:00",
    }

    effect = adapter.create_application(payload)

    applications = json.loads((root / "data" / "applications.json").read_text(encoding="utf-8"))
    created = applications[-1]
    assert created["job_id"] == "job-27"
    assert created["current_stage"] == "applied"
    assert created["record_url"] == "https://example.com/jobs/27"
    assert effect.rollback_payload["operation"] == "delete_application"
    with pytest.raises(ValueError, match="already exists"):
        adapter.create_application(payload)
