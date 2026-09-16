"""Bounded read-only detail capture from frozen OfferBiu crawl samples."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.pipeline.isolation import fetch_job_detail_result_isolated
from packages.recruitment_core.jd_capture import assess_jd_capture


def evaluate(paths, output, *, limit=6, timeout=45, fetch=fetch_job_detail_result_isolated):
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / ".data/evals").resolve()):
        raise ValueError("Evaluation output must stay under .data/evals")
    if not 1 <= limit <= 12 or not 1 <= timeout <= 60:
        raise ValueError("Bounded evaluation requires limit 1..12, timeout 1..60")
    output.mkdir(parents=True, exist_ok=True)
    selected = []
    seen = set()
    for path in paths:
        checkpoint = json.loads(Path(path).read_text(encoding="utf-8"))
        for sample in checkpoint.get("samples", []):
            for raw in (sample.get("crawl") or {}).get("raw_jobs", []):
                url = raw.get("detail_url") or raw.get("jd_url") or ""
                if not url or url in seen:
                    continue
                if raw.get("cohort") != 2027 or raw.get("cohort_status") != "confirmed":
                    continue
                selected.append((sample, raw, str(path)))
                seen.add(url)
                break
    results = []
    for index, (sample, raw, checkpoint) in enumerate(selected[:limit]):
        job = {**raw, "careers_url": sample.get("crawl_url") or raw.get("source_list_url")}
        try:
            response = fetch(job, timeout_seconds=timeout)
        except Exception as exc:
            response = {"detail": "", "status": "failed", "error_type": type(exc).__name__}
        assessment = assess_jd_capture({**job, "jd_raw": response.get("detail"),
                                       "capture_evidence": response.get("capture_evidence") or {}})
        row = {"company": sample.get("company"), "title": raw.get("title"),
               "detail_url": raw.get("detail_url") or raw.get("jd_url"),
               "source_checkpoint": checkpoint, "hydration": response,
               "capture_complete": assessment.complete, "reason_code": assessment.reason_code}
        results.append(row)
        (output / f"detail-{index:03}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"company": row["company"], "capture_complete": assessment.complete,
                          "reason_code": assessment.reason_code}), flush=True)
    summary = {"read_only": True, "db_writes": 0, "model_calls": 0,
               "tested_at": datetime.now(timezone.utc).isoformat(), "tested_jobs": len(results),
               "capture_complete": sum(row["capture_complete"] for row in results),
               "reasons": dict(Counter(row["reason_code"] for row in results)),
               "full_company_validation": False}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.checkpoint, args.output, limit=args.limit, timeout=args.timeout)))
