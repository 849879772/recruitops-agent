"""Read and import the legacy autumn recruitment snapshot.

The source is deliberately handled as an immutable input.  SQLite is opened
with ``mode=ro`` and all three source files are fingerprinted before and after
the read.  The only writes performed by :class:`LegacyMigration` are to the
Agent-owned PostgreSQL snapshot tables and the Agent-owned companies export.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterator, Mapping, Sequence

import yaml
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from packages.domain.models import (
    Application,
    ApplicationStage,
    Company,
    Job,
    JobAnalysis,
    RecruitmentBatch,
    ScheduleEvent,
)
from packages.storage import Storage
from packages.storage.sync import (
    upsert_application_snapshot,
    upsert_company_snapshot,
    upsert_job_analysis_snapshot,
    upsert_job_snapshot,
    upsert_schedule_event_snapshot,
)


UTC = timezone.utc
LEGACY_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SNAPSHOT_TABLES = frozenset(
    {
        "company_snapshots",
        "job_snapshots",
        "job_analysis_snapshots",
        "application_snapshots",
        "schedule_event_snapshots",
    }
)


class LegacyMigrationError(RuntimeError):
    """Raised when the legacy snapshot cannot be imported without data loss."""


@dataclass(frozen=True)
class LegacySourcePaths:
    """The three legacy inputs used by the migration."""

    config: Path
    jobs_db: Path
    applications: Path

    @classmethod
    def from_root(cls, root: Path | str) -> "LegacySourcePaths":
        root_path = Path(root).expanduser().resolve()
        return cls(
            config=root_path / "config.yaml",
            jobs_db=root_path / "data" / "jobs.db",
            applications=root_path / "data" / "applications.json",
        )

    def resolved(self) -> "LegacySourcePaths":
        return LegacySourcePaths(
            config=self.config.expanduser().resolve(),
            jobs_db=self.jobs_db.expanduser().resolve(),
            applications=self.applications.expanduser().resolve(),
        )

    @property
    def files(self) -> tuple[Path, Path, Path]:
        return (self.config, self.jobs_db, self.applications)


@dataclass(frozen=True)
class _FileFingerprint:
    size: int
    digest: str


@dataclass(frozen=True)
class LegacySnapshot:
    """Validated, in-memory records ready for a single Agent transaction."""

    companies: tuple[Company, ...]
    jobs: tuple[Job, ...]
    analyses: Mapping[str, JobAnalysis]
    applications: tuple[Application, ...]
    schedule_events: tuple[ScheduleEvent, ...]
    companies_yaml: Mapping[str, Any]
    source_fingerprints: Mapping[Path, _FileFingerprint]
    warnings: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            "companies": len(self.companies),
            "jobs": len(self.jobs),
            "analyses": len(self.analyses),
            "applications": len(self.applications),
            "schedule_events": len(self.schedule_events),
        }


@dataclass(frozen=True)
class MigrationReport:
    """Stable CLI/API result without copying source records into logs."""

    mode: str
    companies: int
    jobs: int
    analyses: int
    applications: int
    schedule_events: int
    source_read_only_verified: bool
    source_unchanged: bool
    database_written: bool
    companies_yaml_written: bool
    companies_yaml_path: str
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "companies": self.companies,
            "jobs": self.jobs,
            "analyses": self.analyses,
            "applications": self.applications,
            "schedule_events": self.schedule_events,
            "source_read_only_verified": self.source_read_only_verified,
            "source_unchanged": self.source_unchanged,
            "database_written": self.database_written,
            "companies_yaml_written": self.companies_yaml_written,
            "companies_yaml_path": self.companies_yaml_path,
            "warnings": list(self.warnings),
        }


def _fingerprint(path: Path) -> _FileFingerprint:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        size = path.stat().st_size
    except OSError as exc:
        raise LegacyMigrationError(f"cannot fingerprint source file: {path}") from exc
    return _FileFingerprint(size=size, digest=digest.hexdigest())


def _fingerprints(paths: LegacySourcePaths) -> dict[Path, _FileFingerprint]:
    return {path: _fingerprint(path) for path in paths.files}


def _assert_fingerprints(
    before: Mapping[Path, _FileFingerprint],
    paths: LegacySourcePaths,
) -> None:
    after = _fingerprints(paths)
    changed = [str(path) for path, fingerprint in before.items() if after[path] != fingerprint]
    if changed:
        raise LegacyMigrationError(
            "source changed during migration; refusing to continue: " + ", ".join(changed)
        )


def _source_sqlite_uri(path: Path) -> str:
    return f"{path.as_uri()}?mode=ro"


def _validate_source_paths(paths: LegacySourcePaths) -> None:
    for path in paths.files:
        if not path.is_file():
            raise LegacyMigrationError(f"source file does not exist: {path}")
    try:
        with sqlite3.connect(_source_sqlite_uri(paths.jobs_db), uri=True) as connection:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise LegacyMigrationError(
            f"source SQLite cannot be opened in read-only mode: {paths.jobs_db}"
        ) from exc


def verify_source_read_only(paths: LegacySourcePaths | Path | str) -> dict[str, Any]:
    """Verify the source can be read without opening it in write mode.

    This check never probes writability by creating a sentinel in the source
    directory.  It verifies the actual safety boundary used by the importer:
    readable files, a SQLite ``mode=ro`` connection, and stable fingerprints.
    """

    normalized = (
        paths.resolved()
        if isinstance(paths, LegacySourcePaths)
        else LegacySourcePaths.from_root(paths).resolved()
    )
    before = _fingerprints(normalized) if all(path.is_file() for path in normalized.files) else {}
    _validate_source_paths(normalized)
    _assert_fingerprints(before, normalized)
    return {
        "sqlite_mode": "ro",
        "source_unchanged": True,
        "files_checked": len(normalized.files),
    }


@contextmanager
def _source_read_guard(paths: LegacySourcePaths) -> Iterator[dict[Path, _FileFingerprint]]:
    _validate_source_paths(paths)
    before = _fingerprints(paths)
    try:
        yield before
    finally:
        _assert_fingerprints(before, paths)


def _text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = _text(value).replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            return None
    return result if result.tzinfo is not None else result.replace(tzinfo=UTC)


def _parse_date(value: Any) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_time(value: Any) -> time | None:
    text = _text(value)
    if not text:
        return None
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).time()
        except ValueError:
            continue
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None or value == "":
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _string_list(value: Any) -> list[str]:
    parsed = _json_value(value)
    if isinstance(parsed, list):
        return [_text(item) for item in parsed if _text(item)]
    text = _text(value)
    return [text] if text else []


def _dict_list(value: Any) -> list[dict[str, Any]]:
    parsed = _json_value(value)
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _dict_value(value: Any) -> dict[str, Any]:
    parsed = _json_value(value)
    return parsed if isinstance(parsed, dict) else {}


def _integer(value: Any, *, field: str, allow_none: bool = True) -> int | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise LegacyMigrationError(f"missing integer field: {field}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise LegacyMigrationError(f"invalid integer field {field}: {value!r}") from exc


def _stage(value: Any) -> ApplicationStage:
    try:
        return ApplicationStage(_text(value, default="applied"))
    except ValueError:
        return ApplicationStage.APPLIED


def _source_rows(config_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise LegacyMigrationError(f"cannot read legacy config: {config_path}") from exc
    if not isinstance(raw, dict):
        raise LegacyMigrationError("legacy config must be a YAML mapping")
    rows = raw.get("companies")
    if rows is None:
        raise LegacyMigrationError("legacy config does not contain companies")
    if not isinstance(rows, list):
        raise LegacyMigrationError("legacy config companies must be a list")
    return raw, [row for row in rows if isinstance(row, dict) and _text(row.get("name"))]


def _company_id(row: Mapping[str, Any], index: int) -> str:
    return _text(row.get("id") or row.get("key"), default=f"config-{index}")


def _company_yaml_row(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    result: dict[str, Any] = {"id": _company_id(row, index), "name": _text(row["name"])}
    fields = (
        "aliases",
        "careers_url",
        "campaign_url",
        "campaign_urls",
        "campaign_text",
        "crawler",
        "link_kind",
    )
    for field in fields:
        value = row.get(field)
        if value is None or value == "":
            continue
        if field == "aliases":
            result[field] = _string_list(value)
        elif field == "campaign_urls" and not isinstance(value, list):
            result[field] = [str(value)]
        else:
            result[field] = value
    result["integration_status"] = _text(
        row.get("integration_status"),
        default="connected" if _text(row.get("crawler")) else "not_connected",
    )
    return result


def _company_model(row: Mapping[str, Any], index: int) -> Company:
    company_id = _company_id(row, index)
    crawler = _optional_text(row.get("crawler") or row.get("crawler_key"))
    integration_status = _text(
        row.get("integration_status"),
        default="connected" if crawler else "not_connected",
    )
    return Company(
        id=company_id,
        name=_text(row["name"]),
        aliases=_string_list(row.get("aliases")),
        campus_url=_optional_text(row.get("careers_url") or row.get("campus_url")),
        crawler_key=crawler,
        integration_status=integration_status,
        source="config.yaml",
        source_ref=f"companies[{index}]",
        created_at=LEGACY_EPOCH,
        updated_at=LEGACY_EPOCH,
    )


def _company_identity_lookup(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Map legacy company names and aliases to stable Agent company IDs."""

    lookup: dict[str, str] = {}
    for index, row in enumerate(rows):
        company_id = _company_id(row, index)
        identities = [company_id, _text(row.get("name")), *_string_list(row.get("aliases"))]
        for identity in identities:
            key = _text(identity).casefold()
            if key:
                lookup.setdefault(key, company_id)
    return lookup


def _historical_company_id(name: str) -> str:
    digest = hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()[:16]
    return f"legacy-job-company-{digest}"


def _read_sqlite_rows(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        with sqlite3.connect(_source_sqlite_uri(path), uri=True) as connection:
            connection.row_factory = sqlite3.Row
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "jobs" not in tables:
                raise LegacyMigrationError("legacy SQLite does not contain jobs table")
            jobs = [dict(row) for row in connection.execute("SELECT * FROM jobs ORDER BY id")]
            analyses = []
            if "job_analysis" in tables:
                analyses = [
                    dict(row)
                    for row in connection.execute("SELECT * FROM job_analysis ORDER BY job_id")
                ]
            return jobs, analyses
    except LegacyMigrationError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise LegacyMigrationError(f"cannot read legacy SQLite: {path}") from exc


def _analysis_model(row: Mapping[str, Any]) -> JobAnalysis:
    score = _integer(row.get("match_score"), field="job_analysis.match_score")
    return JobAnalysis(
        match_score=score,
        advantages=_string_list(row.get("advantages")),
        gaps=_string_list(row.get("gaps")),
        summary=_optional_text(row.get("summary")),
        recommendation=_optional_text(row.get("recommendation")),
        score_breakdown=_dict_value(row.get("score_breakdown")),
        evidence=_dict_list(row.get("evidence")),
        evidence_level=_optional_text(row.get("evidence_level")),
        matched_directions=_string_list(row.get("matched_directions")),
        primary_match_direction=_optional_text(row.get("primary_match_direction")),
        analysis_status=_optional_text(row.get("analysis_status")),
        model=_optional_text(row.get("model")),
        analyzed_at=_parse_datetime(row.get("analyzed_at")),
    )


def _job_model(
    row: Mapping[str, Any],
    analysis: JobAnalysis | None,
    company_identity_lookup: Mapping[str, str] | None = None,
) -> Job:
    job_id = _text(row.get("id"))
    company = _text(row.get("company"))
    company_id = (company_identity_lookup or {}).get(company.casefold(), company)
    detail_url = _text(row.get("jd_url"))
    if not job_id:
        raise LegacyMigrationError("legacy jobs contains a row without id")
    if not company:
        raise LegacyMigrationError(f"legacy job {job_id} has no company")
    if not detail_url:
        raise LegacyMigrationError(f"legacy job {job_id} has no jd_url")
    crawled_at = _parse_datetime(row.get("crawled_at"))
    last_seen_at = _parse_datetime(row.get("last_seen_at"))
    created_at = crawled_at or last_seen_at or LEGACY_EPOCH
    updated_at = last_seen_at or crawled_at or LEGACY_EPOCH
    track = _text(row.get("recruitment_track"), default="unknown").lower()
    try:
        batch = RecruitmentBatch(track)
    except ValueError:
        batch = RecruitmentBatch.UNKNOWN
    cohort = _integer(row.get("cohort"), field=f"jobs[{job_id}].cohort")
    if cohort == 0:
        cohort = None
    match_score = analysis.match_score if analysis is not None else _integer(
        row.get("match_score"), field=f"jobs[{job_id}].match_score"
    )
    return Job(
        id=job_id,
        company_id=company_id,
        title=_text(row.get("title")),
        city=_optional_text(row.get("city")),
        detail_url=detail_url,
        jd_raw=_optional_text(row.get("jd_raw")),
        cohort=cohort,
        cohort_status=_text(row.get("cohort_status"), default="unconfirmed"),
        batch=batch,
        match_score=match_score,
        first_seen_at=crawled_at,
        last_seen_at=last_seen_at,
        source=_text(row.get("source"), default=company),
        source_ref=job_id,
        created_at=created_at,
        updated_at=updated_at,
    )


def _load_applications(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyMigrationError(f"cannot read legacy applications: {path}") from exc
    if not isinstance(raw, list):
        raise LegacyMigrationError("legacy applications must be a JSON list")
    return [row for row in raw if isinstance(row, dict)]


def _application_model(row: Mapping[str, Any]) -> Application:
    app_id = _text(row.get("id"))
    if not app_id:
        raise LegacyMigrationError("legacy applications contains a row without id")
    applied_at = _parse_datetime(row.get("applied_at"))
    updated_at = _parse_datetime(row.get("updated_at"))
    created_at = applied_at or updated_at or LEGACY_EPOCH
    return Application(
        id=app_id,
        company_name=_text(row.get("company")),
        job_title=_text(row.get("title")),
        job_id=_optional_text(row.get("job_id")),
        record_url=_optional_text(row.get("record_url")),
        stage=_stage(row.get("current_stage")),
        idempotency_key=f"application:{app_id}",
        note=_optional_text(row.get("note")),
        stage_history=_dict_list(row.get("stages")),
        source_stage=_optional_text(row.get("source_stage")),
        source_status=_optional_text(row.get("source_status")),
        source_status_synced_at=_parse_datetime(row.get("source_status_synced_at")),
        source="applications.json",
        source_ref=app_id,
        created_at=created_at,
        updated_at=updated_at or created_at,
    )


def _schedule_model(
    event: Mapping[str, Any],
    application_model: Application,
    event_id: str,
) -> ScheduleEvent | None:
    event_date = _parse_date(event.get("event_date"))
    if event_date is None:
        return None
    event_type = _text(event.get("event_type"), default="日程")
    created_at = (
        _parse_datetime(event.get("created_at"))
        or application_model.updated_at
        or LEGACY_EPOCH
    )
    title = _text(event.get("title"), default=f"{event_type} · {application_model.company_name}")
    return ScheduleEvent(
        id=event_id,
        title=title,
        event_date=event_date,
        event_time=_parse_time(event.get("event_time")),
        event_type=event_type,
        company_name=application_model.company_name,
        job_title=application_model.job_title,
        application_stage=application_model.stage,
        starts_at=_parse_datetime(event.get("starts_at")),
        ends_at=_parse_datetime(event.get("ends_at")),
        application_id=application_model.id,
        location_or_link=_optional_text(
            event.get("location_or_link") or event.get("location") or event.get("link")
        ),
        note=_optional_text(event.get("note")),
        source="applications.json",
        source_ref=f"{application_model.id}/events/{event_id}",
        created_at=created_at,
        updated_at=_parse_datetime(event.get("updated_at")) or created_at,
    )


class LegacySnapshotReader:
    """Read all legacy records before any Agent-owned write is attempted."""

    def __init__(self, paths: LegacySourcePaths):
        self.paths = paths.resolved()

    def read(self) -> LegacySnapshot:
        with _source_read_guard(self.paths) as source_fingerprints:
            _, raw_company_rows = _source_rows(self.paths.config)
            companies = tuple(
                _company_model(row, index) for index, row in enumerate(raw_company_rows)
            )
            company_ids = [company.id for company in companies]
            if len(set(company_ids)) != len(company_ids):
                raise LegacyMigrationError("legacy config contains duplicate company ids")
            company_identity_lookup = _company_identity_lookup(raw_company_rows)
            warnings: list[str] = []

            raw_jobs, raw_analyses = _read_sqlite_rows(self.paths.jobs_db)
            unmatched_companies = sorted(
                {
                    _text(row.get("company"))
                    for row in raw_jobs
                    if _text(row.get("company")).casefold() not in company_identity_lookup
                }
            )
            historical_yaml_rows: list[dict[str, Any]] = []
            if unmatched_companies:
                historical_companies = []
                for name in unmatched_companies:
                    company_id = _historical_company_id(name)
                    company_identity_lookup[name.casefold()] = company_id
                    historical_companies.append(
                        Company(
                            id=company_id,
                            name=name,
                            aliases=[],
                            crawler_key=None,
                            integration_status="not_connected",
                            source="jobs.db",
                            source_ref=f"historical-company:{name}",
                            created_at=LEGACY_EPOCH,
                            updated_at=LEGACY_EPOCH,
                        )
                    )
                    historical_yaml_rows.append(
                        {
                            "id": company_id,
                            "name": name,
                            "integration_status": "not_connected",
                        }
                    )
                companies = (*companies, *historical_companies)
                warnings.append(
                    f"created {len(unmatched_companies)} historical companies from jobs.db"
                )
            analysis_rows: dict[str, JobAnalysis] = {}
            for row in raw_analyses:
                job_id = _text(row.get("job_id"))
                if not job_id:
                    raise LegacyMigrationError("job_analysis contains a row without job_id")
                if job_id in analysis_rows:
                    raise LegacyMigrationError(f"duplicate analysis for legacy job {job_id}")
                analysis_rows[job_id] = _analysis_model(row)

            jobs = tuple(
                _job_model(
                    row,
                    analysis_rows.get(_text(row.get("id"))),
                    company_identity_lookup,
                )
                for row in raw_jobs
            )
            job_ids = [job.id for job in jobs]
            if len(set(job_ids)) != len(job_ids):
                raise LegacyMigrationError("legacy jobs contains duplicate ids")
            orphaned_analyses = set(analysis_rows) - set(job_ids)
            if orphaned_analyses:
                raise LegacyMigrationError(
                    "job_analysis contains jobs missing from jobs: "
                    + ", ".join(sorted(orphaned_analyses))
                )

            raw_applications = _load_applications(self.paths.applications)
            applications: list[Application] = []
            schedule_events: list[ScheduleEvent] = []
            application_ids: set[str] = set()
            event_ids: set[str] = set()
            for raw_application in raw_applications:
                application = _application_model(raw_application)
                if application.id in application_ids:
                    raise LegacyMigrationError(
                        f"legacy applications contains duplicate id {application.id}"
                    )
                application_ids.add(application.id)
                applications.append(application)
                raw_events = raw_application.get("events") or []
                if not isinstance(raw_events, list):
                    warnings.append(f"application {application.id} events is not a list; skipped")
                    continue
                for index, raw_event in enumerate(raw_events):
                    if not isinstance(raw_event, dict):
                        warnings.append(f"application {application.id} event {index} is not an object")
                        continue
                    event_id = _text(raw_event.get("id")) or (
                        f"{application.id}:{_text(raw_event.get('event_date'))}:"
                        f"{_text(raw_event.get('event_type'), default='日程')}"
                    )
                    if event_id in event_ids:
                        raise LegacyMigrationError(f"legacy schedule contains duplicate id {event_id}")
                    event_model = _schedule_model(raw_event, application, event_id)
                    if event_model is None:
                        warnings.append(
                            f"application {application.id} event {event_id} has invalid date; skipped"
                        )
                        continue
                    event_ids.add(event_id)
                    schedule_events.append(event_model)

            yaml_rows = [
                _company_yaml_row(row, index) for index, row in enumerate(raw_company_rows)
            ]
            yaml_rows.extend(historical_yaml_rows)
            return LegacySnapshot(
                companies=companies,
                jobs=jobs,
                analyses=analysis_rows,
                applications=tuple(applications),
                schedule_events=tuple(schedule_events),
                companies_yaml={"companies": yaml_rows},
                source_fingerprints=source_fingerprints,
                warnings=tuple(warnings),
            )


def _sqlite_target_path(database_url: str) -> Path | None:
    try:
        url = make_url(database_url)
    except Exception:
        return None
    if not url.drivername.startswith("sqlite") or not url.database:
        return None
    if url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()


def _reject_source_targets(paths: LegacySourcePaths, output_path: Path, database_url: str) -> None:
    protected = {path.resolve() for path in paths.files}
    if output_path.resolve() in protected:
        raise LegacyMigrationError("companies output must not replace a legacy source file")
    database_path = _sqlite_target_path(database_url)
    if database_path is not None and database_path in protected:
        raise LegacyMigrationError("Agent SQLite target must not be a legacy source file")


def _yaml_text(payload: Mapping[str, Any]) -> str:
    return yaml.safe_dump(
        dict(payload),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _stage_yaml(output_path: Path, payload: Mapping[str, Any]) -> Path:
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        )
        temporary = Path(handle.name)
        with handle:
            handle.write(_yaml_text(payload))
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except OSError as exc:
        raise LegacyMigrationError(f"cannot stage companies export: {output_path}") from exc


def _ensure_snapshot_tables(storage: Storage) -> None:
    try:
        tables = set(inspect(storage.engine).get_table_names())
    except Exception as exc:
        raise LegacyMigrationError("cannot inspect Agent snapshot database") from exc
    missing = sorted(SNAPSHOT_TABLES - tables)
    if missing:
        raise LegacyMigrationError(
            "Agent snapshot tables are missing; apply existing Agent migrations first: "
            + ", ".join(missing)
        )


def _write_snapshot(storage: Storage, snapshot: LegacySnapshot) -> None:
    try:
        with storage.transaction() as session:
            for company in snapshot.companies:
                upsert_company_snapshot(session, company)
            for job in snapshot.jobs:
                upsert_job_snapshot(session, job)
                analysis = snapshot.analyses.get(job.id)
                if analysis is not None:
                    upsert_job_analysis_snapshot(session, job, analysis)
            for application in snapshot.applications:
                upsert_application_snapshot(session, application)
            for event in snapshot.schedule_events:
                upsert_schedule_event_snapshot(session, event)
    except Exception as exc:
        raise LegacyMigrationError("Agent snapshot transaction failed") from exc


class LegacyMigration:
    """Plan or apply one deterministic legacy-to-Agent snapshot import."""

    def __init__(
        self,
        paths: LegacySourcePaths,
        *,
        database_url: str,
        companies_output: Path,
    ):
        self.paths = paths.resolved()
        self.database_url = database_url
        self.companies_output = Path(companies_output).expanduser().resolve()

    def run(self, *, mode: str = "dry-run") -> MigrationReport:
        if mode not in {"dry-run", "apply"}:
            raise ValueError("mode must be 'dry-run' or 'apply'")
        _reject_source_targets(self.paths, self.companies_output, self.database_url)
        snapshot = LegacySnapshotReader(self.paths).read()
        database_written = False
        companies_yaml_written = False
        temporary_yaml: Path | None = None
        try:
            if mode == "dry-run":
                return self._report(
                    snapshot,
                    mode=mode,
                    source_unchanged=True,
                    companies_yaml_path=self.companies_output,
                )

            try:
                storage = Storage.from_url(self.database_url, initialize=False)
            except Exception as exc:
                raise LegacyMigrationError("cannot open Agent snapshot database") from exc
            try:
                _ensure_snapshot_tables(storage)
                temporary_yaml = _stage_yaml(self.companies_output, snapshot.companies_yaml)
                _assert_fingerprints(snapshot.source_fingerprints, self.paths)
                _write_snapshot(storage, snapshot)
                database_written = True
            finally:
                storage.engine.dispose()

            try:
                os.replace(temporary_yaml, self.companies_output)
                temporary_yaml = None
                companies_yaml_written = True
            except OSError as exc:
                raise LegacyMigrationError(
                    f"Agent database was updated but companies export could not be installed: "
                    f"{self.companies_output}"
                ) from exc
            _assert_fingerprints(snapshot.source_fingerprints, self.paths)
            return self._report(snapshot, mode=mode, source_unchanged=True,
                                companies_yaml_path=self.companies_output,
                                database_written=database_written,
                                companies_yaml_written=companies_yaml_written)
        finally:
            if temporary_yaml is not None:
                try:
                    temporary_yaml.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _report(
        snapshot: LegacySnapshot,
        *,
        mode: str,
        source_unchanged: bool,
        companies_yaml_path: Path | str = "",
        database_written: bool = False,
        companies_yaml_written: bool = False,
    ) -> MigrationReport:
        return MigrationReport(
            mode=mode,
            **snapshot.counts,
            source_read_only_verified=True,
            source_unchanged=source_unchanged,
            database_written=database_written,
            companies_yaml_written=companies_yaml_written,
            companies_yaml_path=str(companies_yaml_path),
            warnings=snapshot.warnings,
        )


def run_migration(
    *,
    source_root: Path | str | None = None,
    source_config: Path | str | None = None,
    jobs_db: Path | str | None = None,
    applications: Path | str | None = None,
    database_url: str,
    companies_output: Path | str,
    mode: str = "dry-run",
) -> MigrationReport:
    """Convenience API used by the CLI and fixture tests."""

    if source_config is not None or jobs_db is not None or applications is not None:
        root = Path(source_root or ".").expanduser().resolve()
        paths = LegacySourcePaths(
            config=Path(source_config or root / "config.yaml"),
            jobs_db=Path(jobs_db or root / "data" / "jobs.db"),
            applications=Path(applications or root / "data" / "applications.json"),
        )
    else:
        paths = LegacySourcePaths.from_root(source_root or ".")
    migration = LegacyMigration(
        paths,
        database_url=database_url,
        companies_output=Path(companies_output),
    )
    report = migration.run(mode=mode)
    return MigrationReport(
        **{
            **report.as_dict(),
            "companies_yaml_path": str(Path(companies_output).expanduser().resolve()),
            "warnings": tuple(report.warnings),
        }
    )


__all__ = [
    "LegacyMigration",
    "LegacyMigrationError",
    "LegacySnapshot",
    "LegacySnapshotReader",
    "LegacySourcePaths",
    "MigrationReport",
    "run_migration",
    "verify_source_read_only",
]
