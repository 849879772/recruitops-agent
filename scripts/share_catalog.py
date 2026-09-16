"""Export only public catalog data; import atomically into an empty installation."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import MetaData, Table, select, func, DateTime, Date
from packages.config import get_settings
from packages.storage import Storage, Base
from packages.discovery import company_registry  # Registers catalog tables.

TABLES = ("company_snapshots", "job_snapshots", "job_analysis_snapshots", "company_source_records", "company_source_attempts")
CAPTURE_KEYS = {"status", "method", "content_sha256", "identity_verified", "terminal_observed",
                "remaining_controls", "captured_at", "source_url", "detail_url", "failure_reasons"}


def clean(value, private=()):
    if isinstance(value, dict):
        return {k: clean(v, private) for k, v in value.items()
                if not re.search(r"password|cookie|authorization|api.?key|access.?token", k, re.I)}
    if isinstance(value, list):
        return [clean(v, private) for v in value]
    if not isinstance(value, str):
        return value
    for item in private:
        if item:
            value = value.replace(item, "[REDACTED]")
    if value.startswith(("http://", "https://")):
        u = urlsplit(value)
        query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                 if not re.search(r"token|session|cookie|authorization|ticket|openid|password", k, re.I)]
        value = urlunsplit((u.scheme, u.netloc.rsplit("@", 1)[-1], u.path, urlencode(query), u.fragment))
    return value


def public_row(table, row, private=()):
    row = dict(row)
    if "source_ref" in row:
        row["source_ref"] = "shared-catalog:" + str(row.get("id") or row.get("job_id"))
    if table == "job_analysis_snapshots":
        for key in ("advantages", "gaps", "recommendation", "refusal_reason", "profile_fingerprint"):
            row[key] = None
        row["summary"] = "导入的历史参考评分，基于原使用者的求职偏好，并非接收者的个人匹配度。"
        row["evidence"] = []
        row["score_breakdown"] = {}
        row["matched_directions"] = []
        row["primary_match_direction"] = None
        row["filter_reasons"] = []
    if table == "job_snapshots":
        row["capture_evidence"] = {k: v for k, v in (row.get("capture_evidence") or {}).items() if k in CAPTURE_KEYS}
    for key in ("reason", "capture_failure_reason"):
        if row.get(key):
            # Diagnostics may contain local paths or request parameters.
            row[key] = row.get("reason_code") or "historical_capture_failure"
    return clean(row, private)


def export_catalog(storage, directory, private=()):
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(), "tables": {},
                "score_policy": "Historical numeric scores retained; personal explanations and resume evidence removed."}
    metadata = MetaData()
    with storage.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            if connection.dialect.name == "postgresql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            for name in TABLES:
                table = Table(name, metadata, autoload_with=connection)
                path = directory / f"{name}.jsonl.gz"
                count = 0
                with gzip.open(path, "wt", encoding="utf-8") as output:
                    for row in connection.execution_options(stream_results=True).execute(select(table)).mappings():
                        output.write(json.dumps(public_row(name, row, private), ensure_ascii=False, default=str) + "\n")
                        count += 1
                manifest["tables"][name] = {"count": count, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def import_catalog(storage, directory, *, skip_existing=False):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or set(manifest["tables"]) != set(TABLES):
        raise ValueError("Unsupported catalog manifest")
    for name in TABLES:
        path = directory / f"{name}.jsonl.gz"
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["tables"][name]["sha256"]:
            raise ValueError(f"Checksum mismatch: {name}")
    storage.initialize()
    with storage.engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(724019)")
        for name in TABLES:
            if connection.scalar(select(func.count()).select_from(Base.metadata.tables[name])):
                if skip_existing:
                    return {"status": "skipped_existing_catalog", "changed": False}
                raise ValueError("Destination catalog is not empty; import refused. Existing data was not changed.")
        for name in TABLES:
            table = Base.metadata.tables[name]
            count, batch = 0, []
            with gzip.open(directory / f"{name}.jsonl.gz", "rt", encoding="utf-8") as source:
                for line in source:
                    row = json.loads(line)
                    if set(row) - set(table.columns.keys()):
                        raise ValueError(f"Unknown columns: {name}")
                    for column in table.columns:
                        if row.get(column.name) and isinstance(column.type, DateTime):
                            row[column.name] = datetime.fromisoformat(row[column.name])
                        elif row.get(column.name) and isinstance(column.type, Date):
                            row[column.name] = datetime.fromisoformat(row[column.name]).date()
                    batch.append(row)
                    count += 1
                    if len(batch) == 500:
                        connection.execute(table.insert(), batch)
                        batch = []
                if batch:
                    connection.execute(table.insert(), batch)
            if count != manifest["tables"][name]["count"]:
                raise ValueError(f"Count mismatch: {name}")
    return {name: manifest["tables"][name]["count"] for name in TABLES}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("export", "import"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--if-empty", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    storage = Storage.from_url(settings.database_url)
    private = (settings.llm_api_key, settings.mail_imap_username, settings.mail_imap_password)
    result = export_catalog(storage, args.directory, private) if args.mode == "export" else import_catalog(storage, args.directory, skip_existing=args.if_empty)
    print(json.dumps(result, ensure_ascii=False))
