from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from packages.config import Settings
from packages.domain.models import (
    Application,
    ApplicationStage,
    Company,
    Job,
    JobAnalysis,
    JobDetail,
    JobPage,
    RecruitmentBatch,
    ScheduleEvent,
)


class SourceDataError(RuntimeError):
    """The authoritative source is absent or has an incompatible shape."""


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def _json_list(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _json_string_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [str(value)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    return [str(value)]


def _json_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stage(value: Any) -> ApplicationStage:
    try:
        return ApplicationStage(str(value or "applied"))
    except ValueError:
        return ApplicationStage.APPLIED


class AutumnSourceRepository:
    """Fixed, read-only queries over the existing autumn recruitment state."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _connect(self) -> sqlite3.Connection:
        database = self.settings.source_database.resolve()
        if not database.is_file():
            raise SourceDataError(f"Source database does not exist: {database}")
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def search_jobs(
        self,
        *,
        query: str | None = None,
        company: str | None = None,
        cohort: int | None = None,
        cohort_status: str | None = None,
        recruitment_track: str | None = None,
        first_seen_on: date | None = None,
        batches: tuple[RecruitmentBatch, ...] | None = None,
        min_score: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JobPage:
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            clauses.append("(j.title LIKE ? OR j.company LIKE ? OR j.jd_raw LIKE ?)")
            pattern = f"%{query}%"
            params.extend([pattern, pattern, pattern])
        if company:
            clauses.append("j.company = ?")
            params.append(company)
        if cohort is not None:
            clauses.append("j.cohort = ?")
            params.append(cohort)
        if cohort_status:
            clauses.append("j.cohort_status = ?")
            params.append(cohort_status)
        if recruitment_track:
            clauses.append("j.recruitment_track = ?")
            params.append(recruitment_track)
        if batches:
            placeholders = ", ".join("?" for _ in batches)
            clauses.append(f"j.recruitment_track IN ({placeholders})")
            params.extend(batch.value for batch in batches)
        if first_seen_on is not None:
            clauses.append("date(j.crawled_at) = ?")
            params.append(first_seen_on.isoformat())
        if min_score is not None:
            clauses.append("a.match_score >= ?")
            params.append(min_score)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        base = (
            "FROM jobs j LEFT JOIN job_analysis a ON a.job_id = j.id "
            f"{where}"
        )
        with self._connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
            rows = connection.execute(
                "SELECT j.*, a.match_score "
                f"{base} ORDER BY COALESCE(a.match_score, -1) DESC, j.id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return JobPage(
            items=[self._job_from_row(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    def get_job(self, job_id: str) -> JobDetail | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT j.*, a.match_score, a.advantages, a.gaps, a.summary, "
                "a.recommendation, a.score_breakdown, a.evidence, a.evidence_level, "
                "a.matched_directions, a.primary_match_direction, a.analysis_status, "
                "a.model, a.analyzed_at "
                "FROM jobs j LEFT JOIN job_analysis a ON a.job_id = j.id WHERE j.id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        analysis = None
        if row["match_score"] is not None or row["summary"]:
            analysis = JobAnalysis(
                match_score=row["match_score"],
                advantages=_json_string_list(row["advantages"]),
                gaps=_json_string_list(row["gaps"]),
                summary=row["summary"],
                recommendation=row["recommendation"],
                score_breakdown=_json_dict(row["score_breakdown"]),
                evidence=_json_list(row["evidence"]),
                evidence_level=row["evidence_level"] or None,
                matched_directions=_json_string_list(row["matched_directions"]),
                primary_match_direction=row["primary_match_direction"] or None,
                analysis_status=row["analysis_status"] or None,
                model=row["model"] or None,
                analyzed_at=_parse_datetime(row["analyzed_at"]),
            )
        return JobDetail(job=self._job_from_row(row), analysis=analysis)

    def latest_job_seen_at(self) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(COALESCE(last_seen_at, crawled_at)) AS seen_at FROM jobs"
            ).fetchone()
        return _parse_datetime(row["seen_at"]) if row and row["seen_at"] else None

    def list_companies(self) -> list[Company]:
        config_path = self.settings.source_config
        if not config_path.is_file():
            raise SourceDataError(f"Source config does not exist: {config_path}")
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        rows = config.get("companies") or []
        companies: list[Company] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not row.get("name"):
                continue
            crawler = str(row.get("crawler") or "").strip()
            companies.append(
                Company(
                    id=str(row.get("id") or row.get("key") or f"config-{index}"),
                    name=str(row["name"]),
                    aliases=[str(item) for item in row.get("aliases", [])],
                    campus_url=row.get("careers_url") or None,
                    crawler_key=crawler or None,
                    integration_status="connected" if crawler else "not_connected",
                    source="config.yaml",
                    source_ref=f"companies[{index}]",
                )
            )
        return companies

    def list_applications(self) -> list[Application]:
        rows = self._read_applications()
        result: list[Application] = []
        for row in rows:
            app_id = str(row.get("id"))
            result.append(
                Application(
                    id=app_id,
                    job_id=str(row["job_id"]) if row.get("job_id") is not None else None,
                    company_name=str(row.get("company") or ""),
                    job_title=str(row.get("title") or ""),
                    record_url=row.get("record_url") or None,
                    stage=_stage(row.get("current_stage")),
                    idempotency_key=f"application:{app_id}",
                    note=row.get("note") or None,
                    stage_history=[item for item in row.get("stages", []) if isinstance(item, dict)],
                    source_stage=row.get("source_stage") or None,
                    source_status=row.get("source_status") or None,
                    source_status_synced_at=_parse_datetime(row.get("source_status_synced_at")),
                    source="applications.json",
                    source_ref=app_id,
                    created_at=_parse_datetime(row.get("applied_at")) or datetime.now(timezone.utc),
                    updated_at=_parse_datetime(row.get("updated_at")) or datetime.now(timezone.utc),
                )
            )
        return result

    def list_schedule(self, on_date: date | None = None) -> list[ScheduleEvent]:
        events: list[ScheduleEvent] = []
        for app in self._read_applications():
            for event in app.get("events") or []:
                if not isinstance(event, dict) or not event.get("event_date"):
                    continue
                try:
                    event_date = date.fromisoformat(str(event["event_date"]))
                except ValueError:
                    continue
                if on_date is not None and event_date != on_date:
                    continue
                app_id = str(app.get("id"))
                event_id = str(event.get("id") or f"{app_id}:{event_date}:{event.get('event_type', '')}")
                raw_time = str(event.get("event_time") or "").strip()
                try:
                    event_time = datetime.strptime(raw_time, "%H:%M").time() if raw_time else None
                except ValueError:
                    event_time = None
                event_type = str(event.get("event_type") or "日程")
                events.append(
                    ScheduleEvent(
                        id=event_id,
                        title=f"{event_type} · {app.get('company') or ''}",
                        event_date=event_date,
                        event_time=event_time,
                        event_type=event_type,
                        company_name=str(app.get("company") or ""),
                        job_title=str(app.get("title") or ""),
                        application_stage=_stage(app.get("current_stage")),
                        application_id=app_id,
                        location_or_link=event.get("location_or_link") or None,
                        note=event.get("note") or None,
                        source="applications.json",
                        source_ref=f"{app_id}/events/{event_id}",
                    )
                )
        return sorted(events, key=lambda item: (item.event_date, item.event_time is None, item.event_time, item.title))

    def _read_applications(self) -> list[dict[str, Any]]:
        path = self.settings.source_applications
        if not path.is_file():
            raise SourceDataError(f"Source applications do not exist: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceDataError(f"Cannot read source applications: {exc}") from exc
        if not isinstance(data, list):
            raise SourceDataError("Source applications must be a JSON list")
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> Job:
        track = str(row["recruitment_track"] or "unknown")
        try:
            batch = RecruitmentBatch(track)
        except ValueError:
            batch = RecruitmentBatch.UNKNOWN
        return Job(
            id=str(row["id"]),
            company_id=str(row["company"]),
            title=str(row["title"] or ""),
            city=row["city"] or None,
            detail_url=row["jd_url"],
            jd_raw=row["jd_raw"] or None,
            cohort=row["cohort"] or None,
            cohort_status=row["cohort_status"] or "unconfirmed",
            batch=batch,
            match_score=row["match_score"],
            first_seen_at=_parse_datetime(row["crawled_at"]),
            last_seen_at=_parse_datetime(row["last_seen_at"]),
            source=row["source"] or str(row["company"]),
            source_ref=str(row["id"]),
            created_at=_parse_datetime(row["crawled_at"]) or datetime.now(timezone.utc),
            updated_at=_parse_datetime(row["last_seen_at"]) or datetime.now(timezone.utc),
        )
