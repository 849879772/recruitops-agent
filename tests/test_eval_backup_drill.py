from __future__ import annotations

from pathlib import Path

from evals.backup_drill import run_postgres_backup_drill


def test_postgres_backup_drill_is_preview_only_and_does_not_leak_password(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(command, env, _timeout):
        command_list = list(command)
        calls.append((command_list, dict(env)))
        output = next(item.split("=", 1)[1] for item in command_list if item.startswith("--file="))
        Path(output).write_bytes(b"synthetic custom archive")

    report = run_postgres_backup_drill(
        "postgresql+psycopg://user:secret@127.0.0.1:5433/recruitops",
        tmp_path / "state.dump",
        runner=runner,
    )

    assert report.status == "passed"
    assert report.backup_verified is True
    assert report.restore_preview_verified is True
    assert report.restore_apply_called is False
    assert report.destructive_operations == 0
    assert report.runner_calls == 1
    assert report.password_in_command is False
    assert len(calls) == 1
    assert calls[0][0][0] == "pg_dump"
    assert "pg_restore" not in calls[0][0]
    assert "secret" not in report.model_dump_json()
