from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import yaml
from sqlalchemy import select

from packages.domain.models import ApplicationStage
from packages.storage import ApplicationSnapshot, Storage

from .executor import WriteEffect


_STAGE_ORDER = {
    ApplicationStage.INTERESTED: 0,
    ApplicationStage.APPLIED: 1,
    ApplicationStage.ASSESSMENT: 2,
    ApplicationStage.WRITTEN: 3,
    ApplicationStage.INTERVIEW1: 4,
    ApplicationStage.INTERVIEW2: 5,
    ApplicationStage.INTERVIEW3: 6,
    ApplicationStage.HR: 7,
    ApplicationStage.OFFER: 8,
    ApplicationStage.REJECTED: 9,
    ApplicationStage.WITHDRAWN: 9,
}
_TERMINAL = {ApplicationStage.REJECTED, ApplicationStage.WITHDRAWN}
_COMPANY_KEYS = {
    "name",
    "careers_url",
    "crawler",
    "aliases",
    "campaign_url",
    "campaign_text",
}
_CRAWLER_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _http_url(value: object) -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("an HTTP(S) URL is required")
    return text


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


class SourceBackupManager:
    """Create timestamped copies of the two mutable source files before a write."""

    def __init__(self, source_root: Path, backup_root: Path | None = None) -> None:
        self.source_root = source_root.resolve()
        self.backup_root = (backup_root or self.source_root / "backups" / "recruitops-agent").resolve()
        self.last_backup: Path | None = None

    def __call__(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = self.backup_root / stamp
        target.mkdir(parents=True, exist_ok=False)
        for relative in (Path("config.yaml"), Path("data/applications.json")):
            source = self.source_root / relative
            if source.is_file():
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        self.last_backup = target


class AgentApplicationWriteAdapter:
    """Write validated status changes only to the Agent-owned application snapshot."""

    def __init__(self, storage: Storage, crawler_recipe_path: Path | None = None) -> None:
        self.storage = storage
        self.storage.initialize()
        self.crawler_recipe_path = crawler_recipe_path.resolve() if crawler_recipe_path else None
        self._lock = RLock()

    @staticmethod
    def _unsupported() -> None:
        raise RuntimeError("this Agent adapter only supports application stage updates")

    def update_company_config(self, _payload: dict[str, Any]) -> WriteEffect:
        self._unsupported()

    def update_crawler_recipe(self, payload: dict[str, Any]) -> WriteEffect:
        if self.crawler_recipe_path is None:
            raise RuntimeError("crawler recipe storage is not configured")
        company = str(payload.get("company") or "").strip()
        candidate_id = str(payload.get("candidate_id") or "").strip()
        recipe = payload.get("recipe")
        if not company or len(company) > 200 or not re.fullmatch(r"[0-9a-f]{64}", candidate_id):
            raise ValueError("company and candidate_id are required")
        if not isinstance(recipe, dict):
            raise ValueError("validated crawler recipe is required")
        recipe_type = str(recipe.get("type") or "")
        if recipe_type not in {"api_campaigns", "html_list", "dom"}:
            raise ValueError("unsupported crawler recipe type")
        with self._lock:
            path = self.crawler_recipe_path
            bundle = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if not isinstance(bundle, dict):
                raise ValueError("crawler recipe bundle must be an object")
            before = deepcopy(bundle.get(company))
            previous_version = int(before.get("version") or 0) if isinstance(before, dict) else 0
            normalized = {**deepcopy(recipe), "version": previous_version + 1, "candidate_id": candidate_id}
            bundle[company] = normalized
            _atomic_write(path, json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
        return WriteEffect(
            before={"company": company, "recipe": before},
            after={"company": company, "recipe": normalized},
            rollback_payload={"operation": "restore_crawler_recipe", "company": company, "recipe": before},
        )

    def create_application(self, _payload: dict[str, Any]) -> WriteEffect:
        self._unsupported()

    def create_schedule(self, _payload: dict[str, Any]) -> WriteEffect:
        self._unsupported()

    def bind_recruitment_mail(self, payload: dict[str, Any]) -> WriteEffect:
        from packages.recruitment_mail.binding import MailBindingAdapter
        return MailBindingAdapter(self.storage).bind_recruitment_mail(payload)

    def update_application_stage(self, payload: dict[str, Any]) -> WriteEffect:
        application_id = str(payload["application_id"])
        target = ApplicationStage(str(payload["target_stage"]))
        expected = payload.get("current_stage")
        synced_at = payload.get("source_status_synced_at")
        synced_at_value = (
            datetime.fromisoformat(str(synced_at).replace("Z", "+00:00"))
            if synced_at
            else datetime.now().astimezone()
        )
        with self.storage.write_transaction() as session:
            application = session.scalar(
                select(ApplicationSnapshot)
                .where(ApplicationSnapshot.id == application_id)
                .with_for_update()
            )
            if application is None:
                raise ValueError("Agent application record was not found")
            current = ApplicationStage(application.stage)
            idem = str(payload.get("idempotency_key") or "")
            prior = next((item for item in (application.stage_history or [])
                          if idem and item.get("idempotency_key") == idem), None)
            if prior is not None:
                if prior.get("stage") != target.value or current is not target:
                    raise ValueError("replayed evidence conflicts with the current application stage")
                snapshot = {"application_id": application.id, "stage": application.stage,
                            "stage_history": deepcopy(application.stage_history or [])}
                return WriteEffect(before=snapshot, after=snapshot, rollback_payload={})
            if expected is not None and current is not ApplicationStage(str(expected)):
                raise ValueError("application stage changed after approval preview")
            if current in _TERMINAL and target is not current:
                raise ValueError("terminal application stage cannot change")
            if _STAGE_ORDER[target] < _STAGE_ORDER[current]:
                raise ValueError("application stage cannot move backwards")

            mail_record = None
            if payload.get("mail_record_id"):
                from packages.recruitment_mail.storage import RecruitmentMailRecord

                mail_record = session.scalar(select(RecruitmentMailRecord).where(
                    RecruitmentMailRecord.id == str(payload["mail_record_id"])
                ).with_for_update())
                if mail_record is None or str(mail_record.application_id) != application_id:
                    raise ValueError("mail evidence is not bound to this application")
                from packages.recruitment_mail.binding import confirmed_binding_matches, binding_revision
                if (payload.get("mail_content_digest") is not None
                        and mail_record.content_digest != payload["mail_content_digest"]):
                    raise ValueError("mail content changed before status write")
                if (payload.get("mail_binding_revision") is not None
                        and binding_revision(mail_record) != payload["mail_binding_revision"]):
                    raise ValueError("mail binding changed before status write")
                if confirmed_binding_matches(mail_record, application) is False:
                    raise ValueError("confirmed mail identity changed before status write")

            before = {
                "application_id": application.id,
                "stage": application.stage,
                "source_stage": application.source_stage,
                "source_status": application.source_status,
                "source_status_synced_at": (
                    application.source_status_synced_at.isoformat()
                    if application.source_status_synced_at
                    else None
                ),
                "stage_history": deepcopy(application.stage_history or []),
            }
            history = list(application.stage_history or [])
            history.append(
                {
                    "stage": target.value,
                    "result": str(payload.get("result") or "进行中"),
                    "date": synced_at_value.date().isoformat(),
                    "note": str(payload.get("note") or "官网状态自动复核").strip(),
                    "source": str(payload.get("source") or "edge_application_status_review"),
                    "source_ref": str(payload.get("source_ref") or ""),
                    "idempotency_key": str(payload.get("idempotency_key") or ""),
                    "audit_id": str(payload.get("audit_id") or ""),
                    "event_time": str(payload.get("event_time") or ""),
                }
            )
            application.stage = target.value
            application.stage_history = history
            application.source_stage = str(payload.get("source_stage") or "") or None
            application.source_status = str(payload.get("source_status") or "") or None
            application.source_status_synced_at = synced_at_value
            if payload.get("note"):
                application.note = str(payload["note"])
            application.updated_at = datetime.now().astimezone()
            if mail_record is not None:
                mail_record.processing_status = "processed_updated"
                mail_record.processing_error = None
                mail_record.processed_at = application.updated_at
            session.flush()
            after = {
                "application_id": application.id,
                "stage": application.stage,
                "source_stage": application.source_stage,
                "source_status": application.source_status,
                "source_status_synced_at": application.source_status_synced_at.isoformat(),
                "stage_history": deepcopy(application.stage_history),
            }
        return WriteEffect(
            before=before,
            after=after,
            rollback_payload={"operation": "restore_agent_application", "application": before},
        )


class AutumnSystemWriteAdapter:
    """Three explicit approved writes against the existing local systems."""

    def __init__(
        self,
        source_root: Path,
    ) -> None:
        self.source_root = source_root.resolve()
        self.config_path = self.source_root / "config.yaml"
        self.applications_path = self.source_root / "data" / "applications.json"
        self._lock = RLock()

    def update_company_config(self, payload: dict[str, Any]) -> WriteEffect:
        company = payload.get("company", payload)
        if not isinstance(company, dict) or not company:
            raise ValueError("company payload is required")
        unknown = set(company) - _COMPANY_KEYS
        if unknown:
            raise ValueError(f"unsupported company fields: {sorted(unknown)}")
        name = str(company.get("name") or "").strip()
        crawler = str(company.get("crawler") or "").strip()
        careers_url = _http_url(company.get("careers_url"))
        if not name or len(name) > 200:
            raise ValueError("company name is required")
        if not _CRAWLER_KEY_RE.fullmatch(crawler):
            raise ValueError("invalid crawler key")
        normalized: dict[str, Any] = {
            "name": name,
            "careers_url": careers_url,
            "crawler": crawler,
        }
        for optional in ("campaign_url", "campaign_text"):
            if company.get(optional):
                normalized[optional] = (
                    _http_url(company[optional])
                    if optional.endswith("_url")
                    else str(company[optional]).strip()
                )
        aliases = company.get("aliases")
        if aliases is not None:
            if not isinstance(aliases, list) or not all(
                isinstance(alias, str) and alias.strip() for alias in aliases
            ):
                raise ValueError("aliases must be a list of non-empty strings")
            normalized["aliases"] = [alias.strip() for alias in aliases]

        with self._lock:
            original = self.config_path.read_text(encoding="utf-8")
            config = yaml.safe_load(original) or {}
            companies = config.get("companies")
            if not isinstance(companies, list):
                raise ValueError("config.yaml does not contain a companies list")
            if any(str(item.get("name") or "").casefold() == name.casefold() for item in companies):
                raise ValueError("company already exists")
            rendered = yaml.safe_dump(
                [normalized],
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ).rstrip()
            lines = original.splitlines()
            company_line = next(
                index for index, line in enumerate(lines) if line.rstrip() == "companies:"
            )
            boundary = next(
                (
                    index
                    for index in range(company_line + 1, len(lines))
                    if lines[index].strip() and not lines[index][0].isspace()
                    and not lines[index].startswith("-")
                ),
                len(lines),
            )
            while boundary > company_line + 1 and not lines[boundary - 1].strip():
                boundary -= 1
            updated = "\n".join(lines[:boundary] + rendered.splitlines() + lines[boundary:]) + "\n"
            parsed = yaml.safe_load(updated) or {}
            if not any(item.get("name") == name for item in parsed.get("companies", [])):
                raise ValueError("company config validation failed")
            _atomic_write(self.config_path, updated)
        return WriteEffect(
            before=None,
            after={"company": normalized},
            rollback_payload={"operation": "remove_company", "name": name},
        )

    def update_crawler_recipe(self, _payload: dict[str, Any]) -> WriteEffect:
        raise RuntimeError("the legacy-system adapter cannot update Agent crawler recipes")

    def update_application_stage(self, payload: dict[str, Any]) -> WriteEffect:
        application_id = int(payload["application_id"])
        target = ApplicationStage(str(payload["target_stage"]))
        expected = payload.get("current_stage")
        result = str(payload.get("result") or "待").strip()
        note = str(payload.get("note") or "").strip()
        with self._lock:
            applications = self._load_applications()
            application = self._find_application(applications, application_id)
            before = deepcopy(application)
            current = ApplicationStage(str(application.get("current_stage") or "interested"))
            if expected is not None and current is not ApplicationStage(str(expected)):
                raise ValueError("application stage changed after approval preview")
            if current in _TERMINAL and target is not current:
                raise ValueError("terminal application stage cannot change")
            if _STAGE_ORDER[target] < _STAGE_ORDER[current]:
                raise ValueError("application stage cannot move backwards")
            application.setdefault("stages", []).append(
                {
                    "stage": target.value,
                    "result": result,
                    "date": date.today().isoformat(),
                    "note": note,
                }
            )
            application["current_stage"] = (
                ApplicationStage.REJECTED.value if result == "挂" else target.value
            )
            if payload.get("source_stage") is not None:
                application["source_stage"] = str(payload["source_stage"])
            if payload.get("source_status") is not None:
                application["source_status"] = str(payload["source_status"])
            if payload.get("source_status_synced_at") is not None:
                application["source_status_synced_at"] = str(
                    payload["source_status_synced_at"]
                )
            if note:
                application["note"] = note
            application["updated_at"] = datetime.now().isoformat()
            self._save_applications(applications)
        return WriteEffect(
            before=before,
            after=deepcopy(application),
            rollback_payload={"operation": "replace_application", "application": before},
        )

    def create_application(self, payload: dict[str, Any]) -> WriteEffect:
        job_id = str(payload.get("job_id") or "").strip()
        company = str(payload.get("company") or "").strip()
        title = str(payload.get("title") or "").strip()
        record_url = _http_url(payload.get("record_url"))
        source_job_url = _http_url(payload.get("source_job_url"))
        note = str(payload.get("note") or "").strip()
        if not job_id or not company or not title:
            raise ValueError("job_id, company, and title are required")
        if len(job_id) > 200 or len(company) > 200 or len(title) > 500:
            raise ValueError("application identity fields are too long")
        if len(note) > 1_000:
            raise ValueError("application note is too long")
        captured_at = str(payload.get("captured_at") or datetime.now().isoformat())
        with self._lock:
            applications = self._load_applications()
            if any(str(item.get("job_id") or "") == job_id for item in applications):
                raise ValueError("application already exists for this job")
            application_id = max(
                (int(item.get("id", 0)) for item in applications if str(item.get("id", "")).isdigit()),
                default=0,
            ) + 1
            application = {
                "id": application_id,
                "job_id": job_id,
                "company": company,
                "title": title,
                "city": str(payload.get("city") or "").strip(),
                "record_url": record_url,
                "source_job_url": source_job_url,
                "current_stage": ApplicationStage.APPLIED.value,
                "stages": [
                    {
                        "stage": ApplicationStage.APPLIED.value,
                        "result": "待",
                        "date": date.today().isoformat(),
                        "note": "由 RecruitOps 浏览器扩展记录",
                    }
                ],
                "events": [],
                "note": note,
                "applied_at": captured_at,
                "updated_at": datetime.now().isoformat(),
                "source": "recruitops_browser_capture",
            }
            applications.append(application)
            self._save_applications(applications)
        return WriteEffect(
            before=None,
            after=deepcopy(application),
            rollback_payload={"operation": "delete_application", "application_id": application_id},
        )

    def create_schedule(self, payload: dict[str, Any]) -> WriteEffect:
        application_id = int(payload["application_id"])
        event_date = date.fromisoformat(str(payload["event_date"]))
        event_time = str(payload.get("event_time") or "").strip()
        if event_time:
            datetime.strptime(event_time, "%H:%M")
        event_type = str(payload.get("event_type") or "笔试").strip()
        note = str(payload.get("note") or "").strip()
        with self._lock:
            applications = self._load_applications()
            application = self._find_application(applications, application_id)
            events = application.setdefault("events", [])
            event_id = max((int(item.get("id", 0)) for item in events), default=0) + 1
            event = {
                "id": event_id,
                "event_type": event_type,
                "event_date": event_date.isoformat(),
                "event_time": event_time,
                "note": note,
                "created_at": datetime.now().isoformat(),
            }
            events.append(event)
            application["updated_at"] = datetime.now().isoformat()
            self._save_applications(applications)
        return WriteEffect(
            before=None,
            after={"application_id": application_id, "event": event},
            rollback_payload={
                "operation": "delete_schedule",
                "application_id": application_id,
                "event_id": event_id,
            },
        )

    def _load_applications(self) -> list[dict[str, Any]]:
        value = json.loads(self.applications_path.read_text(encoding="utf-8"))
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ValueError("applications.json must contain a list of objects")
        return value

    def _save_applications(self, applications: list[dict[str, Any]]) -> None:
        _atomic_write(
            self.applications_path,
            json.dumps(applications, ensure_ascii=False, indent=2) + "\n",
        )

    @staticmethod
    def _find_application(
        applications: list[dict[str, Any]], application_id: int
    ) -> dict[str, Any]:
        for application in applications:
            if int(application.get("id", -1)) == application_id:
                return application
        raise KeyError("application was not found")


__all__ = ["AutumnSystemWriteAdapter", "SourceBackupManager"]
