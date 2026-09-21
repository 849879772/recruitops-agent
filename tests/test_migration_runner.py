from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from packages.storage import create_storage_engine
from scripts.apply_migrations import MigrationError, apply_migrations, split_sql
import scripts.apply_migrations as migration_runner
from packages.security.source_guard import UnsafeTargetError


def _migration(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def test_migrations_are_applied_once_and_recorded_in_order(tmp_path: Path) -> None:
    _migration(
        tmp_path,
        "001_create_items.sql",
        "CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT);"
        " INSERT INTO items (id, value) VALUES (1, 'first;value');",
    )
    _migration(
        tmp_path,
        "002_add_status.sql",
        "ALTER TABLE items ADD COLUMN status TEXT NOT NULL DEFAULT 'ready';",
    )
    engine = create_storage_engine(f"sqlite:///{tmp_path / 'agent.db'}")

    first = apply_migrations(engine, tmp_path)
    second = apply_migrations(engine, tmp_path)

    assert first.applied == ("001_create_items.sql", "002_add_status.sql")
    assert first.skipped == ()
    assert second.applied == ()
    assert second.skipped == ("001_create_items.sql", "002_add_status.sql")
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT version, name FROM schema_migrations ORDER BY version")
        ).all()
        item = connection.execute(text("SELECT value, status FROM items")).one()
    assert rows == [
        (1, "001_create_items.sql"),
        (2, "002_add_status.sql"),
    ]
    assert item == ("first;value", "ready")
    engine.dispose()


def test_failed_migration_rolls_back_ddl_and_ledger_insert(tmp_path: Path) -> None:
    _migration(tmp_path, "001_create_items.sql", "CREATE TABLE items (id INTEGER PRIMARY KEY);")
    _migration(
        tmp_path,
        "002_broken.sql",
        "CREATE TABLE transient_items (id INTEGER PRIMARY KEY); INVALID SQL;",
    )
    engine = create_storage_engine(f"sqlite:///{tmp_path / 'agent.db'}")

    with pytest.raises(MigrationError, match="002_broken.sql"):
        apply_migrations(engine, tmp_path)

    with engine.connect() as connection:
        ledger = connection.execute(text("SELECT version FROM schema_migrations")).scalars().all()
        tables = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).scalars().all()
    assert ledger == [1]
    assert "transient_items" not in tables
    engine.dispose()


def test_changed_applied_migration_is_rejected(tmp_path: Path) -> None:
    migration = tmp_path / "001_create_items.sql"
    migration.write_text("CREATE TABLE items (id INTEGER PRIMARY KEY);", encoding="utf-8")
    engine = create_storage_engine(f"sqlite:///{tmp_path / 'agent.db'}")

    apply_migrations(engine, tmp_path)
    migration.write_text(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT);",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="ledger mismatch"):
        apply_migrations(engine, tmp_path)
    engine.dispose()


def test_split_sql_preserves_semicolons_in_literals_and_dollar_quotes() -> None:
    statements = split_sql(
        "INSERT INTO items (value) VALUES ('a;b'); "
        "DO $$ BEGIN PERFORM 'c;d'; END $$;"
    )

    assert statements == (
        "INSERT INTO items (value) VALUES ('a;b')",
        "DO $$ BEGIN PERFORM 'c;d'; END $$",
    )


def test_migrations_reject_sqlite_targets_inside_legacy_source(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _migration(migrations, "001_create_items.sql", "CREATE TABLE items (id INTEGER);")
    target = source / "data" / "other.db"
    monkeypatch.setattr(migration_runner, "DEFAULT_SOURCE_ROOT", source)

    with pytest.raises(UnsafeTargetError):
        apply_migrations(f"sqlite:///{target}", migrations)

    assert target.exists() is False


def test_source_inside_project_is_not_exempt_from_migration_guard(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    source.mkdir()
    monkeypatch.setattr(migration_runner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(migration_runner, "DEFAULT_SOURCE_ROOT", source)
    with pytest.raises(UnsafeTargetError):
        migration_runner._guard_database_target(f"sqlite:///{source / 'other.db'}")


def test_nested_agent_state_remains_allowed_but_source_state_is_protected(monkeypatch, tmp_path):
    project = tmp_path / "agent"
    project.mkdir()
    monkeypatch.setattr(migration_runner, "PROJECT_ROOT", project)
    monkeypatch.setattr(migration_runner, "DEFAULT_SOURCE_ROOT", tmp_path)
    migration_runner._guard_database_target(f"sqlite:///{project / 'agent.db'}")
    with pytest.raises(UnsafeTargetError):
        migration_runner._guard_database_target(f"sqlite:///{tmp_path / 'legacy.db'}")
