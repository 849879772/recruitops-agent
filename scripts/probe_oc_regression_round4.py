"""Run a small, resumable read-only precheck for the 2026-09-05 OC regression."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.discovery import consolidate_source_leads, filter_oc_snapshot
from packages.tools.oc_candidates import OcCandidateCrawlBatchInput, OcCandidateRunner
from scripts.run_oc_full_crawl_eval import _lead_key


SNAPSHOT = ROOT / ".data" / "discovery" / "givemeoc_latest.json"
BASELINE = ROOT / ".data" / "evals" / "oc_feishu_timeout_final.json"
OUTPUT = ROOT / ".data" / "evals" / "regression-20260905-round4"
RESULTS = OUTPUT / "results"
EXPECTED_TARGET_COUNT = 47
SCHEMA = 1


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def port_probe(port: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return {"port": port, "reachable": True, "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}
    except Exception as exc:  # Local evidence only; do not turn it into a site verdict.
        return {
            "port": port,
            "reachable": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def selected_rows(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        row for row in baseline.get("results", [])
        if (
            row.get("integration_status") == "needs_adapter"
            and row.get("error_code") != "activity_empty"
        ) or row.get("integration_status") == "no_eligible_jobs"
    ]
    if len(rows) != EXPECTED_TARGET_COUNT:
        raise ValueError(f"expected {EXPECTED_TARGET_COUNT} targets, found {len(rows)}")
    return rows


def historical_key(lead: Any, old_row: dict[str, Any]) -> tuple[str, str]:
    """Reproduce the old _lead_key when the old run had no inferred crawler URL."""

    current = _lead_key(lead)
    if current == str(old_row.get("lead_key") or ""):
        return current, "current_run_oc_full_crawl_eval._lead_key"
    if old_row.get("crawler_key") == "render":
        value = f"{lead.canonical_name}{chr(0)}".encode("utf-8")
        legacy = hashlib.sha256(value).hexdigest()
        if legacy == str(old_row.get("lead_key") or ""):
            return legacy, "historical_no_inferred_url_key"
    raise ValueError(
        f"lead_key mismatch for {old_row.get('company')!r}: "
        f"current={current}, old={old_row.get('lead_key')}"
    )


def stage_flags(row: dict[str, Any]) -> dict[str, Any]:
    raw = int(row.get("raw_job_count") or 0)
    complete = int(row.get("complete_jd_count") or 0)
    incomplete = int(row.get("incomplete_jd_count") or 0)
    pagination = row.get("pagination_complete") is True
    completeness_known = row.get("completeness_known") is True
    empty_verified = raw == 0 and pagination and completeness_known
    return {
        "fetch": {
            "resolved": raw > 0 or empty_verified,
            "observed_jobs": raw,
            "empty_verified": empty_verified,
        },
        "pagination": {
            "resolved": pagination and (raw > 0 or empty_verified),
            "pagination_complete": pagination,
            "completeness_known": completeness_known,
        },
        "jd": {
            "applicable": raw > 0,
            "resolved": raw > 0 and complete == raw and incomplete == 0,
            "complete_jd_count": complete,
            "incomplete_jd_count": incomplete,
        },
    }


def environment_failure(row: dict[str, Any]) -> bool:
    text = " ".join(
        str(value or "")
        for value in (
            row.get("error_code"),
            row.get("error_message"),
            *(row.get("termination_reasons") or []),
        )
    ).casefold()
    markers = (
        "proxyerror",
        "proxy connection",
        "err_proxy",
        "tunnel connection failed",
        "connection refused",
        "winerror 10061",
        "127.0.0.1:7897",
        "localhost:7897",
        "cannot connect to proxy",
    )
    return any(marker in text for marker in markers)


def build_manifest(baseline: dict[str, Any], snapshot_result: Any, leads: list[Any], *, snapshot_hash: str, baseline_hash: str) -> dict[str, Any]:
    old_rows = selected_rows(baseline)
    by_key: dict[str, tuple[Any, str]] = {}
    for lead in leads:
        by_key[_lead_key(lead)] = (lead, "current_run_oc_full_crawl_eval._lead_key")
        legacy = hashlib.sha256(f"{lead.canonical_name}{chr(0)}".encode("utf-8")).hexdigest()
        by_key.setdefault(legacy, (lead, "historical_no_inferred_url_key"))
    targets = []
    for row in old_rows:
        key = str(row["lead_key"])
        lead_and_mode = by_key.get(key)
        if lead_and_mode is None:
            raise ValueError(f"baseline lead_key not found in current snapshot: {key}")
        lead, _ = lead_and_mode
        mapped_key, mapping_mode = historical_key(lead, row)
        if mapped_key != key:
            raise ValueError(f"internal lead_key mapping mismatch: {mapped_key} != {key}")
        targets.append({
            "lead_key": key,
            "current_lead_key": _lead_key(lead),
            "lead_key_mapping_mode": mapping_mode,
            "company": str(lead.canonical_name),
            "source_url": str(row.get("source_url") or ""),
            "source_projects": list(row.get("source_projects") or []),
            "old": row,
            "scope": (
                "needs_adapter_history"
                if row.get("integration_status") == "needs_adapter"
                else "no_eligible_jobs_recheck"
            ),
        })
    return {
        "schema": SCHEMA,
        "scope": "baseline needs_adapter except activity_empty plus all baseline no_eligible_jobs",
        "target_count": len(targets),
        "read_only": True,
        "model_calls": 0,
        "database_writes": 0,
        "config_writes": 0,
        "snapshot": {
            "path": str(SNAPSHOT),
            "sha256": snapshot_hash,
            "captured_at": snapshot_result.captured_at,
            "rows_seen": snapshot_result.rows_seen,
            "pages_fetched": snapshot_result.pages_fetched,
        },
        "baseline": {"path": str(BASELINE), "sha256": baseline_hash},
        "environment_preflight": {
            "HTTP_PROXY_expected": "http://127.0.0.1:7897",
            "HTTPS_PROXY_expected": "http://127.0.0.1:7897",
            "ports": [port_probe(7897), port_probe(8012)],
        },
        "runner_request": {
            "require_complete_jd": True,
            "include_job_evidence": True,
            "per_company_timeout_seconds": 60,
            "hydrate_details": False,
            "workers": 1,
            "retry_policy": "one runner invocation per lead; no script-level retry",
        },
        "targets": targets,
    }


def choose_precheck_targets(manifest: dict[str, Any], count: int) -> list[dict[str, Any]]:
    targets = list(manifest["targets"])
    preferred: list[dict[str, Any]] = []
    for predicate in (
        lambda row: row["company"] == "TP-LINK",
        lambda row: row["old"].get("error_code") == "activity_empty",
        lambda row: row["old"].get("crawler_key") == "feishu",
    ):
        for target in targets:
            if target in preferred or not predicate(target):
                continue
            preferred.append(target)
            break
    for target in targets:
        if target not in preferred:
            preferred.append(target)
    return preferred[:count]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=47)
    parser.add_argument("--budget-seconds", type=int, default=1200)
    args = parser.parse_args()
    if not 1 <= args.count <= EXPECTED_TARGET_COUNT:
        parser.error(f"--count must be between 1 and {EXPECTED_TARGET_COUNT}")
    if args.budget_seconds <= 0:
        parser.error("--budget-seconds must be positive")

    snapshot_hash = sha256_path(SNAPSHOT)
    baseline_hash = sha256_path(BASELINE)
    baseline = read_json(BASELINE)
    snapshot_result = filter_oc_snapshot(SNAPSHOT)
    leads = list(consolidate_source_leads(snapshot_result.leads))

    manifest_path = OUTPUT / "manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        if manifest.get("snapshot", {}).get("sha256") != snapshot_hash or manifest.get("baseline", {}).get("sha256") != baseline_hash:
            raise ValueError("existing round4 manifest hash does not match current inputs")
    else:
        manifest = build_manifest(
            baseline,
            snapshot_result,
            leads,
            snapshot_hash=snapshot_hash,
            baseline_hash=baseline_hash,
        )
        atomic_write(manifest_path, manifest)

    by_key = {_lead_key(lead): lead for lead in leads}
    by_key.update({target["lead_key"]: by_key[target["current_lead_key"]] for target in manifest["targets"]})
    checkpoint_path = OUTPUT / "checkpoint.json"
    checkpoint = read_json(checkpoint_path) if checkpoint_path.exists() else {
        "schema": SCHEMA,
        "manifest_sha256": sha256_path(manifest_path),
        "completed": [],
        "in_flight": None,
        "consecutive_environment_failures": 0,
        "circuit_breaker": None,
        "interrupted_attempts": [],
        "attempt_counts": {},
        "unattempted": [target["lead_key"] for target in manifest["targets"]],
    }
    if checkpoint.get("manifest_sha256") != sha256_path(manifest_path):
        raise ValueError("checkpoint belongs to a different manifest")

    runner = OcCandidateRunner(SNAPSHOT, ROOT / "config" / "companies.yaml")
    target_by_key = {target["lead_key"]: target for target in manifest["targets"]}
    completed = set(checkpoint.get("completed") or [])
    attempt_counts = checkpoint.setdefault("attempt_counts", {})
    interrupted = checkpoint.get("in_flight")
    if interrupted and interrupted.get("lead_key") not in completed:
        interrupted_key = str(interrupted.get("lead_key") or "")
        checkpoint.setdefault("interrupted_attempts", []).append({
            "lead_key": interrupted_key,
            "company": interrupted.get("company"),
            "started_at": interrupted.get("started_at"),
            "interrupted_at": now(),
            "reason": "previous session stopped before its independent result was persisted",
        })
        attempt_counts[interrupted_key] = max(1, int(attempt_counts.get(interrupted_key) or 0))
    checkpoint["in_flight"] = None
    atomic_write(checkpoint_path, checkpoint)
    selected = choose_precheck_targets(manifest, args.count)
    print(json.dumps({"event": "precheck_started", "target_count": 47, "selected": [t["company"] for t in selected], "completed": len(completed)}, ensure_ascii=False), flush=True)
    session_started = time.monotonic()

    for target in selected:
        key = target["lead_key"]
        if key in completed:
            continue
        if checkpoint.get("circuit_breaker"):
            break
        if time.monotonic() - session_started >= args.budget_seconds:
            checkpoint["stop_reason"] = "budget_exhausted"
            checkpoint["unattempted"] = [target["lead_key"] for target in manifest["targets"] if target["lead_key"] not in completed]
            atomic_write(checkpoint_path, checkpoint)
            break
        if int(attempt_counts.get(key) or 0) >= 2:
            checkpoint.setdefault("attempt_limit_skips", []).append({
                "lead_key": key,
                "company": target["company"],
                "skipped_at": now(),
                "reason": "one interrupted attempt plus one allowed rerun already consumed",
            })
            continue
        lead = by_key.get(key)
        if lead is None:
            raise ValueError(f"lead disappeared from snapshot: {key}")
        started = time.monotonic()
        attempt_counts[key] = int(attempt_counts.get(key) or 0) + 1
        checkpoint["in_flight"] = {"lead_key": key, "company": target["company"], "started_at": now()}
        checkpoint["attempt_counts"] = attempt_counts
        atomic_write(checkpoint_path, checkpoint)
        try:
            result = runner.crawl_lead(
                lead,
                OcCandidateCrawlBatchInput(
                    require_complete_jd=True,
                    include_job_evidence=True,
                    per_company_timeout_seconds=60,
                ),
                hydrate_details=False,
            )
            new = result.model_dump(mode="json")
            error = None
        except Exception as exc:  # Preserve one lead's evidence without retrying it.
            new = {
                "company": target["company"],
                "status": "failed",
                "integration_status": "needs_adapter",
                "error_code": "probe_runner_exception",
                "error_message": str(exc)[-1000:],
                "raw_job_count": 0,
                "accepted_count": 0,
                "complete_jd_count": 0,
                "incomplete_jd_count": 0,
                "pagination_complete": False,
                "completeness_known": False,
                "job_evidence": [],
            }
            error = repr(exc)
        environment_blocked = environment_failure(new)
        old = target["old"]
        record = {
            "schema": SCHEMA,
            "read_only": True,
            "model_calls": 0,
            "database_writes": 0,
            "config_writes": 0,
            "lead_key": key,
            "company": target["company"],
            "source_url": target["source_url"],
            "completed_at": now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "snapshot_sha256": snapshot_hash,
            "baseline_sha256": baseline_hash,
            "proxy_context": {
                "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
                "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
                "ALL_PROXY": os.environ.get("ALL_PROXY"),
                "PYTHONIOENCODING": os.environ.get("PYTHONIOENCODING"),
            },
            "previous": old,
            "result": new,
            "old_new": {
                "old": {"integration_status": old.get("integration_status"), "error_code": old.get("error_code"), "raw_job_count": old.get("raw_job_count"), "accepted_count": old.get("accepted_count"), "complete_jd_count": old.get("complete_jd_count"), "incomplete_jd_count": old.get("incomplete_jd_count"), "pagination_complete": old.get("pagination_complete")},
                "new": {"integration_status": new.get("integration_status"), "error_code": new.get("error_code"), "raw_job_count": new.get("raw_job_count"), "accepted_count": new.get("accepted_count"), "complete_jd_count": new.get("complete_jd_count"), "incomplete_jd_count": new.get("incomplete_jd_count"), "pagination_complete": new.get("pagination_complete")},
            },
            "stage_resolution": {"old": stage_flags(old), "new": stage_flags(new)},
            "environment_blocked": environment_blocked,
            "runner_exception": error,
        }
        atomic_write(RESULTS / f"{key}.json", record)
        completed.add(key)
        checkpoint["completed"] = sorted(completed)
        checkpoint["unattempted"] = [target["lead_key"] for target in manifest["targets"] if target["lead_key"] not in completed]
        if environment_blocked:
            checkpoint["consecutive_environment_failures"] = int(checkpoint.get("consecutive_environment_failures") or 0) + 1
        else:
            checkpoint["consecutive_environment_failures"] = 0
        if checkpoint["consecutive_environment_failures"] >= 3:
            checkpoint["circuit_breaker"] = {
                "reason": "three consecutive same-environment network failures",
                "stopped_at": now(),
                "unattempted": checkpoint["unattempted"],
            }
        checkpoint["in_flight"] = None
        checkpoint["attempt_counts"] = attempt_counts
        atomic_write(checkpoint_path, checkpoint)
        print(json.dumps({"event": "precheck_result", "company": target["company"], "lead_key": key, "error_code": new.get("error_code"), "integration_status": new.get("integration_status"), "raw_job_count": new.get("raw_job_count"), "pagination_complete": new.get("pagination_complete"), "environment_blocked": environment_blocked}, ensure_ascii=False), flush=True)

    print(json.dumps({"event": "precheck_finished", "completed": len(checkpoint.get("completed") or []), "unattempted": len(checkpoint.get("unattempted") or []), "circuit_breaker": checkpoint.get("circuit_breaker")}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
