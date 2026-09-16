"""Read-only incremental JD evaluation; never crawls lists or invokes models/DBs.

Sources are COMPANY_ID=raw.json pairs. --resume reuses the frozen selection;
--replay DIR verifies and re-evaluates saved worker responses without networking.
Only local files below .data/evals are written by the CLI.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.pipeline.isolation import (
    IsolatedOperationTimeout,
    fetch_job_detail_result_isolated,
)
from packages.recruitment_core.job_cohorts import (
    annotate_company_jobs,
    is_confirmed_current,
    trusted_source_campaign,
)
from packages.recruitment_core.job_details import is_jd_incomplete
from packages.recruitment_core.job_filters import (
    is_displayable_campus_job,
    is_doctorate_only_job,
    is_job_record_noise,
)

ROUND2 = ROOT / ".data/evals/regression-20260905-round2"
DEFAULT_SOURCES = [
    f"oc-22c49239b61d0910={ROUND2 / 'moka-pipeline/raw.json'}",
    f"config-506={ROUND2 / 'bigo-fixed/raw.json'}",
]
DEFAULT_COMPLETED = [ROUND2 / f"{name}-jd-accepted/report.json" for name in ("bigo", "legou")]
OC_LABEL = "OC 2027\u5c4a\u79cb\u62db\u6388\u6743\u6765\u6e90"
SCHEMA = 1


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def job_url(job: dict) -> str:
    return str(job.get("jd_url") or job.get("detail_url") or "").strip()


def completed_index(reports: list[dict]) -> set[tuple[str, str]]:
    # Pipeline job_id is an internal row key, NEVER an official native ID.
    return {
        (str(company["company_id"]), str(row["detail_url"]))
        for report in reports for company in report.get("results", [])
        for row in company.get("jd_results", [])
        if row.get("status") == "complete" and row.get("detail_url")
    }


def prepare_job(raw: dict, run: dict, company_id: str, oc_config: dict | None) -> tuple[dict, str]:
    job = deepcopy(raw)
    source_url = str(run.get("source_url") or run.get("crawl_source_url") or "")
    observed = bool(run.get("jobs")) and any(
        row.get("source_url") == source_url and row.get("observed_total", 0) > 0
        and not row.get("fetch_failed")
        for row in run.get("source_runs", [])
    )
    persisted_oc = (
        observed and job.get("cohort_source") == OC_LABEL
        and bool(job.get("cohort_evidence"))
        and job.get("campaign_scope") == "trusted_source_override"
        and job.get("campaign_url") == source_url
    )
    config = oc_config or {}
    bound_config = (
        observed and config.get("id") == company_id
        and config.get("discovery_source") == "oc_snapshot"
        and source_url in config.get("oc_source_urls", [])
        and job.get("company") in [config.get("name"), *(config.get("aliases") or [])]
    )
    if not persisted_oc and not bound_config:
        return job, "oc_source_unbound"
    if "cohort" not in job or "cohort_status" not in job:
        # Reuse the production authorization rule, but only with a bound source.
        authority = {**config, "careers_url": source_url, "source_cohort_url": source_url}
        if persisted_oc:
            authority.update(discovery_source="oc_snapshot", source_cohort_evidence=job["cohort_evidence"])
        campaign = trusted_source_campaign(authority, jobs_observed=observed)
        if campaign:
            job = annotate_company_jobs([job], source_url, inspect_page=False, campaign=campaign)[0]
            job.pop("cohort_checked_at", None)  # Do not randomize the frozen input hash.
    if not is_confirmed_current(job):
        return job, "cohort_ineligible"
    if is_job_record_noise(job) or not is_displayable_campus_job(job) or is_doctorate_only_job(job):
        return job, "role_ineligible"
    if job.get("link_kind") != "detail" or not job_url(job).startswith(("https://", "http://")):
        return job, "not_detail"
    if not is_jd_incomplete(job):
        return job, "already_full"
    return job, "eligible"


def select_samples(sources: list[dict], reports: list[dict], *, per_company: int = 6,
                   oc_configs: list[dict] | None = None) -> dict:
    if not 1 <= per_company <= 6:
        raise ValueError("per_company must be between 1 and 6")
    completed = completed_index(reports)
    configs = {str(row["id"]): row for row in oc_configs or []}
    groups, inventory, seen = [], [], set()
    for source in sources:
        company_id, run = source["company_id"], source["run"]
        eligible = []
        for index, raw in enumerate(run["jobs"]):
            job, reason = prepare_job(raw, run, company_id, configs.get(company_id))
            identity = (company_id, job_url(raw))
            if identity in completed:
                reason = "excluded_completed"
            elif identity in seen:
                reason = "duplicate"
            seen.add(identity)
            worker_hash = digest(job)
            row = {
                "company_id": company_id, "company": raw.get("company", ""),
                "title": raw.get("title", ""), "detail_url": job_url(raw),
                "source_sha256": source["sha256"], "source_index": source["source_index"],
                "raw_index": index, "raw_row_sha256": digest(raw),
                "worker_input_sha256": worker_hash,
                "eval_key": digest([company_id, source["sha256"], index, worker_hash]),
                "selection": reason, "job": job,
            }
            inventory.append(row)
            if reason == "eligible":
                eligible.append(row)
        for row in eligible[per_company:]:
            row["selection"] = "sample_cap"
        for row in eligible[:per_company]:
            row["selection"] = "selected"
        groups.append(eligible[:per_company])
    # Fairness when the global deadline prevents completing both companies.
    selected = [group[i]["eval_key"] for i in range(per_company) for group in groups if i < len(group)]
    return {"per_company": per_company, "inventory": inventory, "selected": selected}


def freeze_inputs(source_specs: list[str], completed_paths: list[Path], output: Path,
                  *, per_company: int, oc_evidence: Path | None = None) -> dict:
    sources, descriptors, reports = [], [], []

    def snapshot(path: Path, kind: str) -> tuple[object, dict]:
        data = path.read_bytes()
        value = json.loads(data)
        relative = f"sources/{len(descriptors):02d}-{kind}.json"
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        descriptor = {"original_path": str(path.resolve()), "path": relative, "sha256": sha256(data), "kind": kind}
        descriptors.append(descriptor)
        return value, descriptor

    company_ids = set()
    for spec in source_specs:
        company_id, separator, path = spec.partition("=")
        if not separator or not company_id or company_id in company_ids:
            raise ValueError("Each source must be a unique COMPANY_ID=raw.json pair")
        company_ids.add(company_id)
        document, descriptor = snapshot(Path(path), "raw")
        sources.append({"company_id": company_id, "run": document[company_id],
                        "sha256": descriptor["sha256"], "source_index": len(descriptors) - 1})
    if len(sources) > 2:
        raise ValueError("At most two companies per bounded evaluation")
    for path in completed_paths:
        report, _ = snapshot(path, "completed")
        reports.append(report)
    configs = []
    if oc_evidence:
        evidence, _ = snapshot(oc_evidence, "oc-evidence")
        configs = evidence["companies"]
    manifest = {
        "schema": SCHEMA, "sources": descriptors, "oc_configs": configs,
        **select_samples(sources, reports, per_company=per_company, oc_configs=configs),
    }
    write_json(output / "inputs.json", manifest)
    return manifest


def load_frozen(directory: Path) -> dict:
    manifest = read_json(directory / "inputs.json")
    if manifest["schema"] != SCHEMA:
        raise ValueError("Unsupported input schema")
    documents = []
    for source in manifest["sources"]:
        path = (directory / source["path"]).resolve()
        if not path.is_relative_to(directory.resolve()) or sha256(path.read_bytes()) != source["sha256"]:
            raise ValueError("Frozen source hash/path mismatch")
        documents.append(read_json(path))
    for row in manifest["inventory"]:
        raw = documents[row["source_index"]][row["company_id"]]["jobs"][row["raw_index"]]
        if digest(raw) != row["raw_row_sha256"] or digest(row["job"]) != row["worker_input_sha256"]:
            raise ValueError("Frozen job hash mismatch")
    return manifest


def evaluate_result(sample: dict, worker: dict) -> dict:
    detail = str(worker.get("detail") or "")
    before = str(sample["job"].get("jd_raw") or "")
    full = not is_jd_incomplete({**sample["job"], "jd_raw": detail})
    identity = worker.get("identity_status")
    identity_verified = identity in {"matched", "request_bound"} and bool(worker.get("identity_evidence"))
    passed = worker.get("status") == "complete" and full and identity_verified
    return {
        "eval_key": sample["eval_key"], "company_id": sample["company_id"],
        "title": sample["title"], "detail_url": sample["detail_url"],
        "raw_index": sample["raw_index"], "source_sha256": sample["source_sha256"],
        "worker_input_sha256": sample["worker_input_sha256"],
        "before_chars": len(before), "after_chars": len(detail),
        "before_sha256": sha256(before.encode("utf-8")), "after_sha256": sha256(detail.encode("utf-8")),
        "status": worker.get("status", "fetch_failed"), "source": worker.get("source", ""),
        "attempts": worker.get("attempts") or [], "error_type": worker.get("error_type", ""),
        "identity_status": identity or "", "identity_evidence": worker.get("identity_evidence") or [],
        "content_complete": full, "identity_verified": identity_verified,
        "verdict": "passed" if passed else "failed",
    }


def summarize(manifest: dict, results: list[dict]) -> dict:
    def counts(rows: list[dict], outcomes: list[dict]) -> dict:
        selections = Counter(row["selection"] for row in rows)
        passed = sum(row["verdict"] == "passed" for row in outcomes)
        tested = len(outcomes)
        return {
            "raw_jobs": len(rows), "excluded_completed": selections["excluded_completed"],
            "eligible_remaining": selections["selected"] + selections["sample_cap"],
            "selected": selections["selected"], "tested": tested, "passed": passed,
            "failed": tested - passed, "not_tested_selected": selections["selected"] - tested,
            "not_selected_sample_cap": selections["sample_cap"],
            "not_tested_remaining": selections["selected"] + selections["sample_cap"] - tested,
            "completeness_rate_tested": passed / tested if tested else None,
            "sample_coverage": tested / selections["selected"] if selections["selected"] else None,
            "selection_counts": dict(selections),
        }
    return {
        **counts(manifest["inventory"], results),
        "companies": {company: counts(
            [row for row in manifest["inventory"] if row["company_id"] == company],
            [row for row in results if row["company_id"] == company],
        ) for company in dict.fromkeys(row["company_id"] for row in manifest["inventory"])},
    }


def saved_results(directory: Path, manifest: dict) -> list[dict]:
    results = []
    for sample in manifest["inventory"]:
        if sample["selection"] != "selected":
            continue
        path = directory / "results" / f"{sample['eval_key']}.json"
        if not path.exists():
            continue
        record = read_json(path)
        if record["worker_input_sha256"] != sample["worker_input_sha256"] or digest(record["worker"]) != record["worker_sha256"]:
            raise ValueError("Saved worker result hash mismatch")
        results.append({**evaluate_result(sample, record["worker"]),
                        "elapsed_seconds": record["elapsed_seconds"], "timeout_seconds": record["timeout_seconds"],
                        "artifact": f"results/{sample['eval_key']}.json", "worker_sha256": record["worker_sha256"]})
    return results


def run_eval(manifest: dict, output: Path, *, timeout_seconds: float = 45,
             budget_seconds: float = 480, fetcher=None, monotonic=time.monotonic,
             cleanup_reserve: float = 15) -> dict:
    if not 0 < timeout_seconds <= 45 or not 0 < budget_seconds <= 480:
        raise ValueError("Maximum per-detail/batch budgets are 45/480 seconds")
    fetcher = fetcher or fetch_job_detail_result_isolated
    checkpoint_path = output / "checkpoint.json"
    manifest_hash = digest(manifest)
    checkpoint = read_json(checkpoint_path) if checkpoint_path.exists() else {
        "inputs_sha256": manifest_hash, "elapsed_seconds": 0.0, "in_flight": None,
        "interrupted": [], "sessions": [],
    }
    if checkpoint["inputs_sha256"] != manifest_hash:
        raise ValueError("Checkpoint belongs to different frozen inputs")
    results = saved_results(output, manifest)
    done = {row["eval_key"] for row in results}
    if checkpoint["in_flight"]:
        pending = checkpoint["in_flight"]
        if pending["eval_key"] not in done:
            checkpoint["interrupted"].append(pending["eval_key"])
            # Charge the reserved slot; interruption cannot grant a fresh budget.
            checkpoint["elapsed_seconds"] += pending["reserved_seconds"]
        checkpoint["in_flight"] = None
    session_start, previous_elapsed = monotonic(), checkpoint["elapsed_seconds"]
    session = {"started_at": datetime.now(timezone.utc).isoformat(), "new_calls": 0,
               "resume_skipped": len(done), "stop_reason": "selection_exhausted"}
    checkpoint["sessions"].append(session)
    samples = {row["eval_key"]: row for row in manifest["inventory"]}
    write_json(checkpoint_path, checkpoint)
    for key in manifest["selected"]:
        if key in done or key in checkpoint["interrupted"]:
            continue
        elapsed = previous_elapsed + monotonic() - session_start
        remaining = budget_seconds - elapsed
        # The production boundary reserves up to 15s for timeout tree cleanup.
        # Do not start a partial slot or label an unstarted slot a failure.
        if remaining < timeout_seconds:
            session["stop_reason"] = "budget_exhausted"
            break
        worker_timeout = timeout_seconds - cleanup_reserve
        if worker_timeout <= 0:
            raise ValueError("Per-detail timeout must exceed cleanup reserve")
        sample = samples[key]
        checkpoint.update(elapsed_seconds=elapsed, in_flight={"eval_key": key, "reserved_seconds": timeout_seconds})
        write_json(checkpoint_path, checkpoint)
        started = monotonic()
        try:
            worker = fetcher(deepcopy(sample["job"]), timeout_seconds=worker_timeout)
        except Exception as exc:
            status = "timeout" if isinstance(exc, (IsolatedOperationTimeout, TimeoutError)) else "fetch_failed"
            worker = {"detail": "", "status": status, "source": "isolated_worker",
                      "attempts": [f"isolated_worker:{status}"], "error_type": type(exc).__name__,
                      "diagnostics_unavailable": "Worker did not return; inner attempts are unknown."}
        duration = monotonic() - started
        record = {"worker_input_sha256": sample["worker_input_sha256"], "worker": worker,
                  "worker_sha256": digest(worker), "elapsed_seconds": duration,
                  "timeout_seconds": worker_timeout, "completed_at": datetime.now(timezone.utc).isoformat()}
        write_json(output / "results" / f"{key}.json", record)
        session["new_calls"] += 1
        checkpoint.update(elapsed_seconds=previous_elapsed + monotonic() - session_start, in_flight=None)
        write_json(checkpoint_path, checkpoint)
        # Only counters and fixed labels go to stdout, never text, URLs or errors.
        print(json.dumps({"new_calls": session["new_calls"], "verdict": evaluate_result(sample, worker)["verdict"]}), flush=True)
    checkpoint["elapsed_seconds"] = previous_elapsed + monotonic() - session_start
    session["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(checkpoint_path, checkpoint)
    results = saved_results(output, manifest)
    report = {"scope": "incremental_jd_only", "read_only": True, "model_calls": 0,
              "database_writes": 0, "list_calls": 0, "workers": 1,
              "inputs_sha256": manifest_hash, "batch_budget_seconds": budget_seconds,
              "per_detail_budget_seconds": timeout_seconds, "cleanup_reserve_seconds": cleanup_reserve,
              "elapsed_seconds": checkpoint["elapsed_seconds"], "session": session,
              "interrupted": checkpoint["interrupted"],
              "summary": summarize(manifest, results), "results": results}
    write_json(output / "report.json", report)
    return report


def replay(directory: Path, output: Path) -> dict:
    manifest = load_frozen(directory)
    results = saved_results(directory, manifest)
    report = {"offline": True, "list_calls": 0, "detail_calls": 0, "model_calls": 0,
              "database_writes": 0, "inputs_sha256": digest(manifest),
              "artifact_directory": str(directory.resolve()),
              "summary": summarize(manifest, results), "results": results}
    write_json(output / "replay-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", help="COMPANY_ID=raw.json; never refetches a list")
    parser.add_argument("--completed", type=Path, action="append", help="Prior accepted pipeline report")
    parser.add_argument("--oc-evidence", type=Path, help="Optional frozen JSON with OC-backed companies")
    parser.add_argument("--output", type=Path, default=ROOT / ".data/evals/regression-20260905-round3/jd")
    parser.add_argument("--per-company", type=int, default=6, choices=range(1, 7))
    parser.add_argument("--timeout-seconds", type=float, default=45)
    parser.add_argument("--budget-seconds", type=float, default=480)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / ".data/evals").resolve()):
        parser.error("Output must remain below .data/evals")
    if args.replay:
        report = replay(args.replay.resolve(), output)
    else:
        if args.resume:
            if args.source or args.completed or args.oc_evidence:
                parser.error("Resume uses frozen inputs; do not supply replacement sources")
            manifest = load_frozen(output)
        else:
            if output.exists() and any(output.iterdir()):
                parser.error("Use --resume or a new directory to preserve prior evidence")
            output.mkdir(parents=True, exist_ok=True)
            manifest = freeze_inputs(args.source or DEFAULT_SOURCES, args.completed or DEFAULT_COMPLETED,
                                     output, per_company=args.per_company, oc_evidence=args.oc_evidence)
        if args.prepare_only:
            report = {"summary": summarize(manifest, [])}
        else:
            report = run_eval(manifest, output, timeout_seconds=args.timeout_seconds, budget_seconds=args.budget_seconds)
    print(json.dumps(report["summary"], ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
