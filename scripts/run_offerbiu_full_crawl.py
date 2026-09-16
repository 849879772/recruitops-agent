"""Resumable whole-snapshot OfferBiu capture; no database or model calls."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.discovery.oc_capture import classify_oc_destination_url
from packages.discovery.offerbiu_registry import INDUSTRY_GROUPS
from scripts.eval_offerbiu_crawl_sample import evaluate_sample, _safe_sample, _write_json


def plan(snapshot):
    if snapshot.get("source") != "offerbiu" or not isinstance(snapshot.get("items"), list):
        raise ValueError("Expected an OfferBiu snapshot")
    tasks = {}
    excluded = []
    for row in snapshot["items"]:
        if (2027 not in row.get("targetYears", []) or row.get("recruitType") != "秋招"
                or not INDUSTRY_GROUPS.intersection(row.get("industryGroupCodes", []))):
            excluded.append(_safe_sample(row))
            continue
        urls = row.get("applyUrl") or [""]
        if isinstance(urls, str):
            urls = [urls]
        for url in dict.fromkeys(urls):
            identity = [str(row.get("companyId") or row.get("companyName")), str(url)]
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
            task = tasks.setdefault(key, {"key": key, "row": {**row, "applyUrl": url}, "sources": []})
            task["sources"].append(_safe_sample(row))
    return list(tasks.values()), excluded


def capture(task, timeout, evaluate=evaluate_sample):
    row = task["row"]
    url = row.get("applyUrl") or ""
    exclusion = classify_oc_destination_url(url) if url else ("missing_entry", "No entry URL")
    if exclusion:
        result = {"company": row.get("companyName"), "crawler_status": "skipped",
                  "reason_code": exclusion[0], "reason": exclusion[1], "crawl": None,
                  "crawl_url": _safe_sample(row).get("applyUrl"), "raw_sample": _safe_sample(row)}
    else:
        try:
            result = evaluate(row, timeout_seconds=timeout)
        except Exception as exc:
            result = {"company": row.get("companyName"), "crawler_status": "error",
                      "error_code": type(exc).__name__, "crawl": None,
                      "raw_sample": _safe_sample(row), "crawl_url": _safe_sample(row).get("applyUrl")}
    result.update(task_key=task["key"], sources=task["sources"], read_only=True,
                  model_calls=0, db_writes=0, finished_at=datetime.now(timezone.utc).isoformat())
    return result


def run(snapshot_path, output, *, workers=3, timeout=120, resume=False, evaluate=evaluate_sample):
    if not 1 <= workers <= 6:
        raise ValueError("workers must be 1..6")
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / ".data/evals").resolve()):
        raise ValueError("Output must stay under .data/evals")
    raw = Path(snapshot_path).read_bytes()
    snapshot = json.loads(raw)
    tasks, excluded = plan(snapshot)
    checkpoint = output / "checkpoints"
    checkpoint.mkdir(parents=True, exist_ok=True)
    manifest = {"snapshot_sha256": hashlib.sha256(raw).hexdigest(),
                "input": str(Path(snapshot_path).resolve()), "source_records": len(snapshot["items"]),
                "task_count": len(tasks), "out_of_scope_count": len(excluded),
                "workers": workers, "browser_slots": os.getenv("RECRUITOPS_BROWSER_SLOTS"),
                "timeout_seconds": timeout, "read_only": True,
                "model_calls": 0, "db_writes": 0, "detail_hydration": "separate_phase"}
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not resume or old["snapshot_sha256"] != manifest["snapshot_sha256"]:
            raise ValueError("Existing run requires --resume and the same snapshot")
    _write_json(manifest_path, manifest)
    _write_json(output / "sources.json", [{**t, "row": _safe_sample(t["row"])} for t in tasks])
    _write_json(output / "out-of-scope.json", excluded)
    results = {}
    for task in tasks:
        path = checkpoint / f"{task['key']}.json"
        if resume and path.exists():
            result = json.loads(path.read_text(encoding="utf-8"))
            if result.get("task_key") != task["key"]:
                raise ValueError("Checkpoint identity mismatch")
            results[task["key"]] = result

    def progress():
        values = list(results.values())
        summary = {**manifest, "finished_entries": len(values),
                   "remaining_entries": len(tasks) - len(values),
                   "scope_complete": len(values) == len(tasks),
                   "status_counts": dict(Counter(r["crawler_status"] for r in values)),
                   "raw_job_rows": sum((r.get("crawl") or {}).get("raw_job_count", 0) for r in values),
                   "pagination_complete_entries": sum((r.get("crawl") or {}).get("pagination_evidence", {}).get("pagination_complete") is True for r in values),
                   "updated_at": datetime.now(timezone.utc).isoformat()}
        _write_json(output / "summary.json", summary)
        return summary

    progress()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="offerbiu-full") as executor:
        futures = {executor.submit(capture, task, timeout, evaluate): task
                   for task in tasks if task["key"] not in results}
        for future in as_completed(futures):
            task = futures[future]
            result = future.result()
            _write_json(checkpoint / f"{task['key']}.json", result)
            results[task["key"]] = result
            summary = progress()
            print(json.dumps({"finished": summary["finished_entries"], "total": len(tasks),
                              "company": result.get("company"), "status": result["crawler_status"],
                              "jobs": (result.get("crawl") or {}).get("raw_job_count", 0)}, ensure_ascii=False), flush=True)
    with (output / "raw-jobs.jsonl").open("w", encoding="utf-8") as handle:
        for task in tasks:
            result = results[task["key"]]
            for job in (result.get("crawl") or {}).get("raw_jobs", []):
                handle.write(json.dumps({"task_key": task["key"], "company": result.get("company"),
                                         "crawl_url": result.get("crawl_url"), "job": job}, ensure_ascii=False) + "\n")
    return progress()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 7), default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 180:
        parser.error("timeout must be 1..180 seconds")
    print(json.dumps(run(args.snapshot, args.output_dir, workers=args.workers,
                         timeout=args.timeout, resume=args.resume), ensure_ascii=False), flush=True)
