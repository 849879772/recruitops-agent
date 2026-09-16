"""Validate migration filenames and reject unsafe SQL before deployment."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


MIGRATION_NAME = re.compile(r"^(?P<version>\d+)_[A-Za-z0-9][A-Za-z0-9_.-]*\.sql$")
DESTRUCTIVE_SQL = re.compile(
    r"\b(?:DROP\s+(?:DATABASE|SCHEMA|TABLE)|TRUNCATE(?:\s+TABLE)?|"
    r"ALTER\s+TABLE\s+\S+\s+DROP)\b",
    re.IGNORECASE,
)
SECRET_LIKE_VALUE = re.compile(
    r"(?:sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
)


def check_migrations(directory: Path) -> list[str]:
    errors: list[str] = []
    if not directory.is_dir():
        return [f"migration directory does not exist: {directory}"]

    files = sorted(directory.glob("*.sql"))
    if not files:
        return [f"no SQL migrations found in {directory}"]

    versions: list[tuple[int, Path]] = []
    for path in files:
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            errors.append(f"invalid migration filename: {path.name}")
            continue
        versions.append((int(match.group("version")), path))

    seen: set[int] = set()
    for version, path in versions:
        if version in seen:
            errors.append(f"duplicate migration version {version:03d}: {path.name}")
        seen.add(version)
    if [version for version, _ in versions] != sorted(version for version, _ in versions):
        errors.append("migration files are not in numeric version order")

    combined_sql: list[str] = []
    for path in files:
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            errors.append(f"empty migration: {path.name}")
        if DESTRUCTIVE_SQL.search(sql):
            errors.append(f"destructive SQL is not allowed in deployment migrations: {path.name}")
        if SECRET_LIKE_VALUE.search(sql):
            errors.append(f"secret-like value found in migration: {path.name}")
        combined_sql.append(sql)

    if not re.search(
        r"CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+vector",
        "\n".join(combined_sql),
        re.IGNORECASE,
    ):
        errors.append("pgvector extension installation is missing")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "migrations",
    )
    args = parser.parse_args(argv)
    errors = check_migrations(args.migrations_dir)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    files = sorted(args.migrations_dir.glob("*.sql"))
    print(f"migration static check passed: {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
