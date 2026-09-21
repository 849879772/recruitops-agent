"""Apply ordered Agent-owned SQL migrations transactionally."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine, URL, make_url

# Keep the documented direct-script invocation working before an editable install.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from packages.storage.database import create_storage_engine
from packages.security.source_guard import ensure_target_outside_source_root


MIGRATION_NAME = re.compile(r"^(?P<version>\d+)_[A-Za-z0-9][A-Za-z0-9_.-]*\.sql$")
DEFAULT_MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
DEFAULT_DATABASE_URL = "postgresql+psycopg://recruitops:recruitops@localhost:5433/recruitops"
DEFAULT_SOURCE_ROOT = Path(os.environ.get("RECRUITOPS_SOURCE_ROOT", "D:/秋招系统"))


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied without risking drift."""


@dataclass(frozen=True)
class MigrationFile:
    version: int
    name: str
    path: Path
    checksum: str


@dataclass(frozen=True)
class MigrationResult:
    applied: tuple[str, ...]
    skipped: tuple[str, ...]

    @property
    def applied_versions(self) -> tuple[int, ...]:
        return tuple(int(name.split("_", 1)[0]) for name in self.applied)


def _guard_database_target(database: Engine | str | URL) -> None:
    url = database.url if isinstance(database, Engine) else make_url(database)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return
    if not DEFAULT_SOURCE_ROOT.is_dir():
        return
    target = Path(url.database)
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    source_root = DEFAULT_SOURCE_ROOT.resolve()
    project_root = PROJECT_ROOT.resolve()
    # Only allow the historical layout where this agent lives below the source.
    # A source nested inside this checkout must remain protected.
    nested_agent = project_root != source_root and project_root.is_relative_to(source_root)
    ensure_target_outside_source_root(
        DEFAULT_SOURCE_ROOT,
        target,
        allowed_root=project_root if nested_agent else None,
    )


def discover_migrations(directory: Path | str) -> tuple[MigrationFile, ...]:
    """Read and validate migration files in numeric version order."""

    root = Path(directory)
    if not root.is_dir():
        raise MigrationError(f"migration directory does not exist: {root}")

    migrations: list[MigrationFile] = []
    seen_versions: set[int] = set()
    for path in root.glob("*.sql"):
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        version = int(match.group("version"))
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version: {version}")
        seen_versions.add(version)
        content = path.read_bytes()
        if not content.strip():
            raise MigrationError(f"empty migration: {path.name}")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"migration is not UTF-8: {path.name}") from exc
        migrations.append(
            MigrationFile(
                version=version,
                name=path.name,
                path=path,
                checksum=hashlib.sha256(content).hexdigest(),
            )
        )

    if not migrations:
        raise MigrationError(f"no SQL migrations found in {root}")
    return tuple(sorted(migrations, key=lambda item: item.version))


def split_sql(sql: str) -> tuple[str, ...]:
    """Split ordinary SQL scripts without breaking quoted semicolons."""

    statements: list[str] = []
    buffer: list[str] = []
    state = "normal"
    dollar_tag = ""
    index = 0

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if state == "line_comment":
            buffer.append(char)
            index += 1
            if char == "\n":
                state = "normal"
            continue

        if state == "block_comment":
            buffer.append(char)
            if char == "*" and next_char == "/":
                buffer.append(next_char)
                index += 2
                state = "normal"
            else:
                index += 1
            continue

        if state == "single_quote":
            buffer.append(char)
            if char == "'":
                if next_char == "'":
                    buffer.append(next_char)
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue

        if state == "double_quote":
            buffer.append(char)
            if char == '"':
                if next_char == '"':
                    buffer.append(next_char)
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue

        if state == "dollar_quote":
            if sql.startswith(dollar_tag, index):
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                state = "normal"
            else:
                buffer.append(char)
                index += 1
            continue

        if char == "-" and next_char == "-":
            buffer.extend((char, next_char))
            index += 2
            state = "line_comment"
            continue
        if char == "/" and next_char == "*":
            buffer.extend((char, next_char))
            index += 2
            state = "block_comment"
            continue
        if char == "'":
            buffer.append(char)
            index += 1
            state = "single_quote"
            continue
        if char == '"':
            buffer.append(char)
            index += 1
            state = "double_quote"
            continue
        if char == "$":
            match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[index:])
            if match:
                dollar_tag = match.group(0)
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                state = "dollar_quote"
                continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer.clear()
            index += 1
            continue

        buffer.append(char)
        index += 1

    statement = "".join(buffer).strip()
    if statement:
        statements.append(statement)
    return tuple(statements)


@contextmanager
def _transaction(engine: Engine):
    """Start a real transaction for both PostgreSQL and sqlite3."""

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            # sqlite3's legacy transaction mode does not begin on DDL alone.
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN")
            yield connection
        except Exception:
            transaction.rollback()
            raise
        else:
            transaction.commit()


def _create_ledger(engine: Engine) -> None:
    with _transaction(engine) as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                checksum VARCHAR(64) NOT NULL,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _apply_one(engine: Engine, migration: MigrationFile) -> bool:
    """Apply one migration and its ledger row in the same transaction."""

    try:
        with _transaction(engine) as connection:
            row = connection.execute(
                text(
                    "SELECT name, checksum FROM schema_migrations "
                    "WHERE version = :version"
                ),
                {"version": migration.version},
            ).mappings().first()
            if row is not None:
                if row["name"] != migration.name or row["checksum"] != migration.checksum:
                    raise MigrationError(
                        f"migration ledger mismatch for version {migration.version}"
                    )
                return False

            for statement in split_sql(migration.path.read_text(encoding="utf-8")):
                connection.exec_driver_sql(statement)
            connection.execute(
                text(
                    "INSERT INTO schema_migrations (version, name, checksum) "
                    "VALUES (:version, :name, :checksum)"
                ),
                {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                },
            )
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(f"migration failed: {migration.name}") from exc
    return True


def apply_migrations(
    database: Engine | str | URL,
    migrations_dir: Path | str = DEFAULT_MIGRATIONS_DIR,
) -> MigrationResult:
    """Apply all pending migrations and return the applied/skipped filenames."""

    _guard_database_target(database)
    migrations = discover_migrations(migrations_dir)
    owns_engine = not isinstance(database, Engine)
    engine = create_storage_engine(database) if owns_engine else database
    applied: list[str] = []
    skipped: list[str] = []
    try:
        _create_ledger(engine)
        for migration in migrations:
            if _apply_one(engine, migration):
                applied.append(migration.name)
            else:
                skipped.append(migration.name)
    finally:
        if owns_engine:
            engine.dispose()
    return MigrationResult(applied=tuple(applied), skipped=tuple(skipped))


run_migrations = apply_migrations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("RECRUITOPS_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy database URL; defaults to RECRUITOPS_DATABASE_URL",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=DEFAULT_MIGRATIONS_DIR,
    )
    args = parser.parse_args(argv)
    try:
        result = apply_migrations(args.database_url, args.migrations_dir)
    except MigrationError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"migration run complete: applied={len(result.applied)} "
        f"skipped={len(result.skipped)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MigrationError",
    "MigrationFile",
    "MigrationResult",
    "apply_migrations",
    "discover_migrations",
    "main",
    "run_migrations",
    "split_sql",
]
