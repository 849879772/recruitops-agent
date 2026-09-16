from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .indexing import SourceDocument
from .ingest import clean_source_text


_ALLOWED_FIELDS = (
    "profile",
    "education",
    "experiences",
    "internships",
    "projects",
    "skills",
    "awards",
    "selfReview",
    "basics",
)
_EXCLUDED_KEYS = frozenset({"name", "email", "phone", "links", "editor"})
_OPTIMIZATION_MARKERS = frozenset({"_optimizationMeta", "_tailoringBaseResume"})


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _resolve_database(path: Path) -> Path:
    try:
        database = path.expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"invalid LuJie SQLite database path: {path}") from exc
    if not database.is_file():
        raise ValueError(f"LuJie SQLite database does not exist: {database}")
    return database


def _open_read_only(database: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
        )
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(
            f"unable to open LuJie SQLite database in read-only mode: {database}"
        ) from exc
    connection.row_factory = sqlite3.Row
    return connection


def _table_name(connection: sqlite3.Connection, expected: str, database: Path) -> str:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    names = {str(row[0]).casefold(): str(row[0]) for row in rows}
    actual = names.get(expected.casefold())
    if actual is None:
        raise ValueError(
            f"LuJie SQLite database is missing required table {expected}: {database}"
        )
    return actual


def _columns(
    connection: sqlite3.Connection,
    table: str,
    record_type: str,
    database: Path,
) -> dict[str, str]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table)})"
    ).fetchall()
    columns = {str(row[1]).casefold(): str(row[1]) for row in rows}
    if not columns:
        raise ValueError(f"LuJie SQLite table {record_type} has no columns: {database}")
    return columns


def _find_column(columns: dict[str, str], *names: str) -> str | None:
    for name in names:
        column = columns.get(name.casefold())
        if column is not None:
            return column
    return None


def _required_column(
    columns: dict[str, str],
    record_type: str,
    database: Path,
    *names: str,
) -> str:
    column = _find_column(columns, *names)
    if column is None:
        raise ValueError(
            f"LuJie SQLite table {record_type} is missing required column "
            f"{names[0]}: {database}"
        )
    return column


def _read_rows(
    connection: sqlite3.Connection,
    table: str,
    id_column: str,
    content_column: str,
    *,
    job_column: str | None = None,
) -> list[sqlite3.Row]:
    selected = (
        f"{_quote_identifier(id_column)} AS record_id, "
        f"{_quote_identifier(content_column)} AS record_content"
    )
    where = ""
    if job_column is not None:
        where = f" WHERE {_quote_identifier(job_column)} IS NULL"
    return connection.execute(
        f"SELECT {selected} FROM {_quote_identifier(table)}{where}"
    ).fetchall()


def _parse_content(
    raw_content: Any,
    *,
    record_type: str,
    record_id: Any,
    database: Path,
) -> dict[str, Any] | None:
    if raw_content is None:
        return None
    if isinstance(raw_content, str) and not raw_content.strip():
        return None
    if isinstance(raw_content, (bytes, bytearray, memoryview)) and not bytes(raw_content).strip():
        return None
    try:
        parsed = json.loads(raw_content)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"invalid JSON content in {record_type}/{record_id} from {database}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"invalid JSON content in {record_type}/{record_id} from {database}: "
            "expected an object"
        )
    return parsed


def _contains_optimization_marker(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in _OPTIMIZATION_MARKERS or _contains_optimization_marker(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_optimization_marker(item) for item in value)
    return False


def _has_substance(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (dict, list, tuple)):
        return bool(value)
    return True


def _sanitize_value(value: Any, *, only_city: bool = False) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            folded_key = key.casefold()
            if key.startswith("_") or folded_key in _EXCLUDED_KEYS:
                continue
            if only_city and folded_key != "city":
                continue
            child_value = _sanitize_value(child, only_city=folded_key == "basics")
            if _has_substance(child_value):
                cleaned[key] = child_value
        return cleaned
    if isinstance(value, list):
        cleaned_items = [_sanitize_value(item) for item in value]
        return [item for item in cleaned_items if _has_substance(item)]
    if isinstance(value, str):
        return value if value.strip() else None
    return value


def _project_content(payload: dict[str, Any]) -> str:
    projected: dict[str, Any] = {}
    for field in _ALLOWED_FIELDS:
        if field not in payload:
            continue
        value = _sanitize_value(payload[field], only_city=field == "basics")
        if _has_substance(value):
            projected[field] = value
    if not projected:
        return ""
    return clean_source_text(json.dumps(projected, ensure_ascii=False, sort_keys=True))


def _document_from_row(
    row: sqlite3.Row,
    *,
    record_type: str,
    database: Path,
) -> tuple[bool, SourceDocument | None]:
    record_id = row["record_id"]
    if record_id is None or not str(record_id).strip():
        raise ValueError(f"LuJie SQLite {record_type} row has no id: {database}")
    payload = _parse_content(
        row["record_content"],
        record_type=record_type,
        record_id=record_id,
        database=database,
    )
    if payload is None:
        return True, None
    if _contains_optimization_marker(payload):
        return False, None
    content = _project_content(payload)
    if not content:
        return True, None
    return True, SourceDocument(
        source="candidate_evidence",
        source_ref=f"{database.as_posix()}#{record_type}/{record_id}",
        content=content,
        metadata={
            "domain": "candidate",
            "kind": "resume",
            "trust": "user_authored",
            "record_type": record_type,
            "record_id": record_id,
        },
    )


def documents_from_lujie_sqlite(
    path: Path,
    *,
    approved_record_ids: Iterable[str] | None = None,
) -> list[SourceDocument]:
    """Extract user-authored candidate evidence from a LuJie SQLite database."""

    database = _resolve_database(path)
    approved = (
        {str(record_id).strip() for record_id in approved_record_ids if str(record_id).strip()}
        if approved_record_ids is not None
        else None
    )
    connection = _open_read_only(database)
    try:
        try:
            version_table = _table_name(connection, "ResumeVersion", database)
            resume_table = _table_name(connection, "Resume", database)

            version_columns = _columns(connection, version_table, "ResumeVersion", database)
            version_id = _required_column(
                version_columns, "ResumeVersion", database, "id"
            )
            version_job = _required_column(
                version_columns, "ResumeVersion", database, "jobId", "job_id"
            )
            version_content = _required_column(
                version_columns, "ResumeVersion", database, "content"
            )
            version_rows = _read_rows(
                connection,
                version_table,
                version_id,
                version_content,
                job_column=version_job,
            )

            raw_documents: list[SourceDocument] = []
            has_original_version = False
            for row in version_rows:
                if approved is not None and str(row["record_id"]) not in approved:
                    continue
                is_original, document = _document_from_row(
                    row,
                    record_type="ResumeVersion",
                    database=database,
                )
                if not is_original:
                    continue
                has_original_version = True
                if document is not None:
                    raw_documents.append(document)
            if has_original_version:
                return raw_documents

            resume_columns = _columns(connection, resume_table, "Resume", database)
            resume_id = _required_column(resume_columns, "Resume", database, "id")
            resume_content = _required_column(
                resume_columns, "Resume", database, "content"
            )
            resume_job = _find_column(resume_columns, "jobId", "job_id")
            resume_rows = _read_rows(
                connection,
                resume_table,
                resume_id,
                resume_content,
                job_column=resume_job,
            )
            documents: list[SourceDocument] = []
            for row in resume_rows:
                if approved is not None and str(row["record_id"]) not in approved:
                    continue
                _, document = _document_from_row(
                    row,
                    record_type="Resume",
                    database=database,
                )
                if document is not None:
                    documents.append(document)
            return documents
        except sqlite3.Error as exc:
            raise ValueError(
                f"invalid LuJie SQLite database or schema: {database}"
            ) from exc
    finally:
        connection.close()


__all__ = ["documents_from_lujie_sqlite"]
