"""Plan or repair the legacy missing-dimensions scoring incident in bounded waves.

Default: read-only plan, no model calls. --apply is required for rescoring/writes.
The JSONL state is private: it contains exact original job/analysis rows for recovery.
Keep the same --state-dir when resuming; validated proposals are reused after a crash.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

import yaml
from sqlalchemy import select, update
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import Settings
from packages.matching import DeepSeekClient, MatchingService
from packages.matching.models import AnalysisRecord, AnalysisStatus
from packages.matching.rules import content_fingerprint, profile_fingerprint
from packages.matching.title_policy import screen_title_job
from packages.recruitment_core.jd_capture import assess_jd_capture
from packages.storage import CompanySnapshot, JobAnalysisSnapshot, JobSnapshot, Storage
from packages.user_settings import CONFIG_FIELDS


LEGACY_PROMPT = "matching-prompt-v1"
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
JOB = JobSnapshot.__table__
ANALYSIS = JobAnalysisSnapshot.__table__


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Candidate:
    job: dict[str, Any]
    analysis: dict[str, Any]
    company: str

    @property
    def snapshot_hash(self) -> str:
        return _hash({"job": self.job, "analysis": self.analysis})

    @property
    def payload(self) -> dict[str, Any]:
        return {**self.job, "company": self.company}


def _is_legacy_incident(analysis: Mapping[str, Any], legacy_model: str) -> bool:
    """Recheck the complete incident predicate on the actual backed-up row."""
    breakdown = analysis.get("score_breakdown")
    return (
        analysis.get("analysis_status") == "complete"
        and analysis.get("prompt_version") == LEGACY_PROMPT
        and analysis.get("model") == legacy_model
        and analysis.get("match_score") is not None
        and isinstance(breakdown, Mapping)
        and all(type(breakdown.get(key)) is int and breakdown[key] == 0
                for key in ("core_direction", "required_skills"))
    )


def build_plan(storage: Storage, profile: Mapping[str, Any], *,
               legacy_model: str = "deepseek-flash", limit: int = DEFAULT_LIMIT,
               job_ids: tuple[str, ...] = (),
               allow_profile_change: bool = False) -> tuple[list[Candidate], dict[str, int]]:
    """Only the specific old-contract incident, never every low-scoring job."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    statement = select(JOB.c.id).join(ANALYSIS, ANALYSIS.c.job_id == JOB.c.id).where(
        ANALYSIS.c.analysis_status == "complete",
        ANALYSIS.c.prompt_version == LEGACY_PROMPT,
        ANALYSIS.c.model == legacy_model,
        ANALYSIS.c.match_score.is_not(None),
        ANALYSIS.c.score_breakdown["core_direction"].as_integer() == 0,
        ANALYSIS.c.score_breakdown["required_skills"].as_integer() == 0,
    ).order_by(JOB.c.id)
    if job_ids:
        statement = statement.where(JOB.c.id.in_(job_ids))
    candidates: list[Candidate] = []
    counts: Counter[str] = Counter()
    with storage.engine.connect() as connection:
        identifiers = connection.execute(statement).scalars().all()
        counts["suspected"] = len(identifiers)
        for identifier in identifiers:
            job_row = connection.execute(select(JOB).where(JOB.c.id == identifier)).mappings().one_or_none()
            analysis_row = connection.execute(select(ANALYSIS).where(ANALYSIS.c.job_id == identifier)).mappings().one_or_none()
            # The ID query and row reads can observe different committed states.
            # CAS below protects later changes, but must never accept a newer,
            # valid score as the original legacy row to replace.
            if job_row is None or analysis_row is None or not _is_legacy_incident(analysis_row, legacy_model):
                counts["skipped_concurrent_change"] += 1
                continue
            job, analysis = dict(job_row), dict(analysis_row)
            company = connection.execute(select(CompanySnapshot.name).where(
                CompanySnapshot.id == job["company_id"])).scalar_one_or_none()
            candidate = Candidate(job, analysis, company or job["company_id"])
            evidence = analysis.get("evidence") or []
            supported = [item for item in evidence if isinstance(item, dict)
                         and item.get("relation") in {"direct", "adjacent"}]
            evidence_broken = not evidence or (bool(supported) and all(
                not str(item.get("profile_evidence") or "").strip() for item in supported))
            if not evidence_broken:
                counts["skipped_evidence_not_incident"] += 1
            elif not allow_profile_change and analysis.get("profile_fingerprint") != profile_fingerprint(profile):
                counts["skipped_profile_changed"] += 1
            elif job["availability_status"] != "active":
                counts["skipped_unavailable"] += 1
            elif not screen_title_job(candidate.payload, profile).eligible:
                counts["skipped_ineligible"] += 1
            elif job["capture_status"] == "failed" or not assess_jd_capture(candidate.payload).complete:
                counts["skipped_incomplete_jd"] += 1
            else:
                counts["eligible"] += 1
                if len(candidates) < limit:
                    candidates.append(candidate)
    counts["selected"] = len(candidates)
    return candidates, dict(counts)


class Journal:
    """Append-only originals and proposals, flushed to disk before database writes."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        if directory.is_symlink():
            raise ValueError("state directory cannot be a link")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "nt":
            from packages.desktop_runtime.instance import secure_directory
            secure_directory(self.directory)
        else:
            self.directory.chmod(0o700)
        self.path = self.directory / "repair.jsonl"
        if self.path.is_symlink():
            raise ValueError("journal cannot be a link")
        self.proposals: dict[str, dict[str, Any]] = {}
        self.originals: set[str] = set()
        if self.path.exists():
            if os.name == "nt":
                from packages.desktop_runtime.instance import secure_directory
                secure_directory(self.path)
            else:
                self.path.chmod(0o600)
            lines = self.path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Never append after a torn line: the operator can retain the
                    # original and copy its complete lines to another state folder.
                    raise ValueError(f"journal is incomplete at line {index + 1}") from None
                if event.get("event") == "original":
                    self.originals.add(event["key"])
                elif event.get("event") == "proposal":
                    self.proposals[event["key"]] = event["record"]

    def append(self, event: Mapping[str, Any]) -> None:
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(_json({"schema": 1, "at": datetime.now(timezone.utc).isoformat(), **event}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


@contextmanager
def _locked_transaction(storage: Storage):
    with storage.engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.begin()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def apply_if_unchanged(storage: Storage, candidate: Candidate, record: AnalysisRecord) -> bool:
    """Lock, compare exact original rows, and replace only a validated complete score."""
    if record.analysis_status is not AnalysisStatus.COMPLETE or record.match_score is None:
        return False
    if record.job_id != candidate.job["id"]:
        raise ValueError("result job identity does not match")
    with _locked_transaction(storage) as connection:
        job = connection.execute(select(JOB).where(JOB.c.id == record.job_id).with_for_update()).mappings().one_or_none()
        analysis = connection.execute(select(ANALYSIS).where(ANALYSIS.c.job_id == record.job_id).with_for_update()).mappings().one_or_none()
        if job is None or analysis is None or _hash({"job": dict(job), "analysis": dict(analysis)}) != candidate.snapshot_hash:
            return False
        now = datetime.now(timezone.utc)
        values = record.model_dump(mode="json")
        values = {key: value for key, value in values.items() if key in ANALYSIS.c and key != "job_id"}
        values.update(advantages=_json(record.advantages), gaps=_json(record.gaps),
                      analyzed_at=record.analyzed_at, updated_at=now)
        connection.execute(update(ANALYSIS).where(ANALYSIS.c.job_id == record.job_id).values(**values))
        connection.execute(update(JOB).where(JOB.c.id == record.job_id).values(
            match_score=record.match_score, updated_at=now))
    return True


def repair(storage: Storage, profile: Mapping[str, Any], service: Any,
           candidates: list[Candidate], *, state_dir: Path, workers: int = 1,
           progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, int]:
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    journal = Journal(state_dir)
    counts: Counter[str] = Counter()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for offset in range(0, len(candidates), workers):
            pending = []
            # Back up the whole bounded wave before issuing calls. All journal
            # writes and database replacements stay on this one coordinating thread.
            for candidate in candidates[offset:offset + workers]:
                metadata = {
                    "job_id": candidate.job["id"], "snapshot_hash": candidate.snapshot_hash,
                    "profile_fingerprint": profile_fingerprint(profile),
                    "content_fingerprint": content_fingerprint(candidate.payload),
                    "input_hash": _hash(candidate.payload),
                    "target_model": getattr(service.client, "model", None),
                    "target_prompt": service.prompt_version,
                    "target_analysis": service.analysis_version,
                }
                key = _hash(metadata)
                if key not in journal.originals:
                    journal.append({"event": "original", "key": key, **metadata,
                                    "job": candidate.job, "analysis": candidate.analysis})
                    journal.originals.add(key)
                pending.append((candidate, key, metadata))
            futures = {
                key: executor.submit(service.analyze_title_first, candidate.payload, profile, existing_analysis=None)
                for candidate, key, _metadata in pending if key not in journal.proposals
            }
            for candidate, key, metadata in pending:
                if key in journal.proposals:
                    record = AnalysisRecord.model_validate(journal.proposals[key])
                    counts["reused_proposal"] += 1
                else:
                    counts["model_jobs"] += 1
                    try:
                        record = futures[key].result().result
                    except Exception:
                        # Arbitrary exceptions may embed endpoint credentials or
                        # candidate facts; retain only a stable diagnostic code.
                        journal.append({"event": "failed", "key": key, **metadata,
                                        "status": "failed", "error_code": "model_call_failed"})
                        counts["failed_preserved"] += 1
                        if progress:
                            progress({"job_id": metadata["job_id"], "state": "failed_preserved", "counts": dict(counts)})
                        continue
                    if record.analysis_status is not AnalysisStatus.COMPLETE or record.match_score is None:
                        journal.append({"event": "failed", "key": key, **metadata,
                                        "status": record.analysis_status.value, "error_code": record.error_code})
                        counts["failed_preserved"] += 1
                        if record.error_code in {"http_401", "http_402", "http_403", "http_429"}:
                            counts["provider_stop"] += 1
                        if progress:
                            progress({"job_id": metadata["job_id"], "state": "failed_preserved", "counts": dict(counts)})
                        continue
                    journal.append({"event": "proposal", "key": key, **metadata,
                                    "record": record.model_dump(mode="json")})
                applied = apply_if_unchanged(storage, candidate, record)
                state = "applied" if applied else "concurrent_change_skipped"
                journal.append({"event": state, "key": key, **metadata})
                counts[state] += 1
                if progress:
                    progress({"job_id": metadata["job_id"], "state": state, "counts": dict(counts)})
            if counts["provider_stop"]:
                break
    return dict(counts)


def load_instance(root: Path, port: int) -> tuple[Storage, Settings, dict[str, Any]]:
    """Read existing settings/secrets only; never open/recover/start an instance."""
    from packages.desktop_runtime.instance import protect_secret
    root = root.resolve(strict=True)
    instance = json.loads((root / "instance.json").read_text(encoding="utf-8"))
    if Path(instance["root"]).resolve() != root or instance.get("schema") != 1:
        raise ValueError("instance identity mismatch")
    password = protect_secret(base64.b64decode(instance["credential"], validate=True), decrypt=True).decode("ascii")
    preferences = json.loads((root / ".data/settings/preferences.json").read_text(encoding="utf-8"))
    settings = Settings(_env_file=None, agent_root=root, **{
        key: value for key, value in preferences.items() if key in CONFIG_FIELDS})
    profile = yaml.safe_load(settings.candidate_profile_config.read_text(encoding="utf-8"))
    profile = profile.get("profile", profile)
    if not isinstance(profile, dict) or not profile:
        raise ValueError("saved candidate profile is missing")
    url = URL.create("postgresql+psycopg", username="desktop", password=password,
                     host="127.0.0.1", port=port, database="postgres")
    return Storage.from_url(url), settings, profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance-root", required=True, type=Path, help="Existing directory containing instance.json")
    parser.add_argument("--db-port", required=True, type=int, help="Current already-running instance PostgreSQL port")
    parser.add_argument("--legacy-model", default="deepseek-flash")
    parser.add_argument("--job-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--allow-profile-change", action="store_true",
                        help="Explicitly rescore old-profile rows against the CURRENT saved profile")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--apply", action="store_true", help="Call saved model and write validated replacements")
    args = parser.parse_args()
    if not 1 <= args.db_port <= 65535:
        parser.error("db-port must be 1..65535")
    storage = None
    try:
        storage, settings, profile = load_instance(args.instance_root, args.db_port)
        candidates, plan = build_plan(storage, profile, legacy_model=args.legacy_model,
                                      limit=args.limit, job_ids=tuple(args.job_id),
                                      allow_profile_change=args.allow_profile_change)
        print(_json({"plan": plan, "apply": args.apply,
                     "selected_job_ids": [item.job["id"] for item in candidates]}))
        if not args.apply or not candidates:
            return 0
        if not settings.llm_enabled or not settings.llm_api_key:
            raise ValueError("saved scoring model is not configured")
        client = DeepSeekClient(api_key=settings.llm_api_key, model=settings.llm_model,
            endpoint=settings.llm_endpoint, api_style=settings.model_api_style,
            timeout=settings.llm_timeout_seconds, max_tokens=settings.llm_matching_max_tokens,
            thinking_enabled=settings.llm_matching_thinking_enabled,
            reasoning_effort=settings.llm_matching_reasoning_effort)
        service = MatchingService(client, max_tokens=settings.llm_matching_max_tokens)
        state_dir = args.state_dir or args.instance_root / "backups/legacy-score-repair"
        print(_json({"result": repair(storage, profile, service, candidates, state_dir=state_dir,
                     workers=args.workers, progress=lambda event: print(_json({"progress": event}), flush=True)),
                     "private_state_dir": str(state_dir.resolve())}))
        return 0
    except Exception as exc:
        # Provider/database exception messages may include credentials or DSNs.
        print(_json({"error": type(exc).__name__, "message": "Repair stopped; original scores preserved for unfinished jobs."}))
        return 2
    finally:
        if storage is not None:
            storage.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
