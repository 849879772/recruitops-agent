from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event, inspect, text

from packages.storage import create_storage_engine
from scripts.apply_migrations import MigrationError, apply_migrations, check_migrations, split_sql
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


def test_check_empty_database_is_read_only_and_does_not_create_ledger(tmp_path: Path) -> None:
    _migration(tmp_path, "001_create_items.sql", "CREATE TABLE items (id INTEGER);")
    engine = create_storage_engine("sqlite:///:memory:")
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, statement, *args: statements.append(statement))
    try:
        plan = check_migrations(engine, tmp_path)

        assert plan.pending == ("001_create_items.sql",)
        assert plan.applied == ()
        assert inspect(engine).get_table_names() == []
        assert all(statement.lstrip().upper().startswith(("SELECT", "PRAGMA")) for statement in statements)
    finally:
        engine.dispose()


def test_check_reports_only_pending_migrations_without_changing_database(tmp_path: Path) -> None:
    _migration(
        tmp_path, "001_create_items.sql",
        "CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT);"
        " INSERT INTO items VALUES (1, 'preserve-me');",
    )
    target = tmp_path / "agent.db"
    database = f"sqlite:///{target}"
    apply_migrations(database, tmp_path)
    previous = target.read_bytes()

    assert check_migrations(database, tmp_path).pending == ()
    _migration(tmp_path, "002_add_status.sql", "ALTER TABLE items ADD COLUMN status TEXT;")
    plan = check_migrations(database, tmp_path)

    assert plan.pending == ("002_add_status.sql",)
    assert plan.applied == ("001_create_items.sql",)
    assert target.read_bytes() == previous


@pytest.mark.parametrize("drift", ["name", "checksum"])
def test_check_and_apply_validate_entire_ledger_before_pending_writes(tmp_path: Path, drift: str) -> None:
    _migration(tmp_path, "001_create_items.sql", "CREATE TABLE items (id INTEGER PRIMARY KEY);")
    _migration(tmp_path, "003_existing.sql", "INSERT INTO items VALUES (3);")
    database = f"sqlite:///{tmp_path / 'agent.db'}"
    apply_migrations(database, tmp_path)
    _migration(tmp_path, "002_pending.sql", "INSERT INTO items VALUES (2);")
    if drift == "name":
        (tmp_path / "003_existing.sql").rename(tmp_path / "003_renamed.sql")
    else:
        _migration(tmp_path, "003_existing.sql", "INSERT INTO items VALUES (4);")
    previous = (tmp_path / "agent.db").read_bytes()

    for operation in (check_migrations, apply_migrations):
        with pytest.raises(MigrationError, match="ledger mismatch for version 3"):
            operation(database, tmp_path)
    assert (tmp_path / "agent.db").read_bytes() == previous


def test_check_and_apply_reject_database_newer_than_package(tmp_path: Path) -> None:
    _migration(tmp_path, "001_create_items.sql", "CREATE TABLE items (id INTEGER);")
    _migration(tmp_path, "002_newer.sql", "ALTER TABLE items ADD COLUMN value TEXT;")
    database = f"sqlite:///{tmp_path / 'agent.db'}"
    apply_migrations(database, tmp_path)
    (tmp_path / "002_newer.sql").unlink()
    previous = (tmp_path / "agent.db").read_bytes()

    for operation in (check_migrations, apply_migrations):
        with pytest.raises(MigrationError, match="version 2 is missing from this package"):
            operation(database, tmp_path)
    assert (tmp_path / "agent.db").read_bytes() == previous


def test_check_missing_sqlite_database_does_not_create_it(tmp_path: Path) -> None:
    _migration(tmp_path, "001_create_items.sql", "CREATE TABLE items (id INTEGER);")
    target = tmp_path / "missing.db"

    with pytest.raises(MigrationError, match="requires an existing database"):
        check_migrations(f"sqlite:///{target}", tmp_path)
    assert not target.exists()


def test_check_only_cli_distinguishes_pending_current_and_invalid_database(tmp_path: Path, capsys) -> None:
    _migration(tmp_path, "001_create_items.sql", "CREATE TABLE items (id INTEGER);")
    target = tmp_path / "agent.db"
    target.touch()
    args = ["--database-url", f"sqlite:///{target}", "--migrations-dir", str(tmp_path)]

    assert migration_runner.main([*args, "--check-only"]) == 10
    assert target.stat().st_size == 0
    assert "pending=1" in capsys.readouterr().out
    assert migration_runner.main(args) == 0
    assert migration_runner.main([*args, "--check-only"]) == 0
    _migration(tmp_path, "001_create_items.sql", "CREATE TABLE changed (id INTEGER);")
    assert migration_runner.main([*args, "--check-only"]) == 1
    assert "ledger mismatch" in capsys.readouterr().err


def test_check_connection_failure_is_not_treated_as_empty_database(tmp_path: Path, monkeypatch) -> None:
    _migration(tmp_path, "001_create_items.sql", "CREATE TABLE items (id INTEGER);")
    engine = create_storage_engine("sqlite:///:memory:")

    def fail_connect():
        raise RuntimeError("private-connection-details")

    monkeypatch.setattr(engine, "connect", fail_connect)
    try:
        with pytest.raises(MigrationError, match="unable to inspect migration ledger") as caught:
            check_migrations(engine, tmp_path)
        assert "private-connection-details" not in str(caught.value)
    finally:
        engine.dispose()


@pytest.mark.parametrize("check_only", [False, True])
def test_cli_connection_setup_failure_does_not_expose_credentials(tmp_path: Path, monkeypatch, capsys, check_only: bool) -> None:
    _migration(tmp_path, "001_create_items.sql", "CREATE TABLE items (id INTEGER);")
    private_url = "postgresql+psycopg://desktop:private-password@127.0.0.1:54321/postgres"

    def fail_engine(*args):
        raise RuntimeError(f"could not connect to {private_url}")

    monkeypatch.setattr(migration_runner, "create_storage_engine", fail_engine)
    args = ["--database-url", private_url, "--migrations-dir", str(tmp_path)]
    if check_only:
        args.append("--check-only")
    assert migration_runner.main(args) == 1
    output = capsys.readouterr()
    assert "unable to access migration database" in output.err
    assert "private-password" not in output.err + output.out


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
