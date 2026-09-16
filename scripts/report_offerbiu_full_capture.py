"""Offline progress and quality report for an OfferBiu full-crawl run."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.discovery.oc_capture import classify_oc_destination_url
from packages.domain.job_identity import normalize_job_identity_url
from packages.recruitment_core.jd_capture import assess_jd_capture

STATUSES = ("has_jobs", "empty", "skip", "error", "pending")
JD_BUCKETS = ("complete", "unknown", "incomplete")
COHORT_BUCKETS = ("2027", "unknown", "other")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
        return value.strip().casefold() == "true"
    return None


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read JSON: {path}: {exc}") from exc


def _write(path: Path, value: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(value, encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _write_json(path: Path, value: Any) -> None:
    _write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _urls(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else [value]
    return list(dict.fromkeys(_text(item) for item in values if _text(item)))


def _sources(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in ("entries", "items", "sources"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, Mapping)]
    return []


def _source_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    for index, item in enumerate(_sources(_json(path))):
        row = _map(item.get("row"))
        nested = item.get("sources") if isinstance(item.get("sources"), list) else []
        nested = [source for source in nested if isinstance(source, Mapping)]
        row = row or (nested[0] if nested else {})
        urls: list[str] = []
        for candidate in (row, *nested):
            urls.extend(_urls(candidate.get("applyUrl") or candidate.get("url") or candidate.get("crawl_url")))
        key = _text(item.get("key") or item.get("source_key") or item.get("task_key"))
        rows.append({
            "source_key": key or f"source-{index + 1}",
            "company": _text(row.get("companyName") or row.get("company") or item.get("company")) or "unknown-company",
            "url": next(iter(dict.fromkeys(urls)), ""),
            "source_urls": list(dict.fromkeys(urls)),
        })
    if not rows:
        raise ValueError(f"sources.json must contain a non-empty list: {path}")
    return rows


def _preclusion(url: str) -> tuple[str, str] | None:
    if not url:
        return "missing_entry", "No public application URL supplied."
    return classify_oc_destination_url(url)


def _load_checkpoints(directory: Path, planned: set[str]) -> tuple[dict[str, tuple[Mapping[str, Any], Path]], dict[str, str], list[str]]:
    found: dict[str, tuple[Mapping[str, Any], Path]] = {}
    errors: dict[str, str] = {}
    extras: list[str] = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        try:
            value = _json(path)
            if not isinstance(value, Mapping):
                raise ValueError("checkpoint must contain an object")
        except ValueError as exc:
            if path.stem in planned:
                errors[path.stem] = str(exc)
            else:
                extras.append(path.name)
            continue
        if isinstance(value.get("eval_sample"), Mapping):
            merged = dict(value)
            merged.update(value["eval_sample"])
            value = merged
        key = _text(value.get("task_key") or value.get("source_key") or path.stem)
        if key not in planned:
            extras.append(path.name)
        elif key not in found:
            found[key] = (value, path)
    return found, errors, extras


def _parts(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[Any], Mapping[str, Any]]:
    crawl = _map(payload.get("crawl"))
    jobs = crawl.get("raw_jobs")
    jobs = jobs if isinstance(jobs, list) else payload.get("raw_jobs", [])
    jobs = jobs if isinstance(jobs, list) else []
    pagination = crawl.get("pagination_evidence")
    pagination = pagination if isinstance(pagination, Mapping) else payload.get("pagination_evidence")
    return crawl, jobs, _map(pagination)


def _value(mapping: Mapping[str, Any], keys: tuple[str, ...], fn: Any) -> Any:
    for key in keys:
        result = fn(mapping.get(key))
        if result is not None:
            return result
    return None


def _jd_bucket(job: Mapping[str, Any]) -> str:
    evidence = job.get("capture_evidence")
    try:
        assessment = assess_jd_capture(job)
    except Exception:
        return "unknown"
    if assessment.complete:
        return "complete"
    if not isinstance(evidence, Mapping) or not evidence:
        return "unknown"
    return "unknown" if _text(evidence.get("status")).casefold() in {"", "unknown"} else "incomplete"


def _cohort_bucket(job: Mapping[str, Any]) -> str:
    value = _int(job.get("cohort"))
    if value is None or value == 0:
        return "unknown"
    return "2027" if value == 2027 else "other"


def _job_url(job: Mapping[str, Any]) -> str:
    return _text(job.get("detail_url") or job.get("jd_url") or job.get("normalized_detail_url"))


def _entry(source: Mapping[str, Any], checkpoint: tuple[Mapping[str, Any], Path] | None,
           read_error: str | None, checkpoint_dir: Path, groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    key, company, url = source["source_key"], source["company"], source["url"]
    exclusion = _preclusion(url)
    base: dict[str, Any] = {
        "source_key": key, "sourcekey": key, "company": company, "url": url,
        "source_urls": source["source_urls"], "pre_excluded": exclusion is not None,
        "pre_exclusion": {"kind": exclusion[0], "reason": exclusion[1]} if exclusion else None,
        "checkpointed": checkpoint is not None or read_error is not None,
        "network_attempted": exclusion is None and (checkpoint is not None or read_error is not None),
        "status": "skip" if exclusion else "pending", "crawler_status": None,
        "reason_code": exclusion[0] if exclusion else "no_checkpoint",
        "reason": exclusion[1] if exclusion else "No checkpoint has been written for this eligible entry.", "crawl_url": url,
        "raw_job_count": 0, "raw_jobs_list_count": 0, "missing_detail_url_count": 0,
        "unique_normalized_detail_url_count": 0,
        "pagination": {"complete": None, "known": None, "complete_and_known": False,
                        "has_more": None, "observed_count": 0, "advertised_count": None,
                        "count_conflict": False, "pages_seen": None, "total_pages": None},
        "jd_counts": {bucket: 0 for bucket in JD_BUCKETS},
        "cohort_counts": {bucket: 0 for bucket in COHORT_BUCKETS},
        "checkpoint": None, "skip_reason": exclusion[1] if exclusion else None,
    }
    if read_error:
        base.update(status="error", reason_code="checkpoint_read_error", reason=read_error)
        return base
    if checkpoint is None:
        return base

    payload, path = checkpoint
    crawl, jobs, evidence = _parts(payload)
    raw_count = _int(crawl.get("raw_job_count"))
    raw_count = len(jobs) if raw_count is None else raw_count
    original = _text(payload.get("crawler_status") or payload.get("status"))
    lowered = original.casefold()
    if exclusion:
        status = "skip"
    elif lowered.startswith("skip"):
        status = "skip"
    elif lowered.startswith(("error", "failed")):
        status = "error"
    elif raw_count > 0 or jobs:
        status = "has_jobs"
    else:
        status = "empty"
    reasons = payload.get("termination_reasons") or crawl.get("termination_reasons") or []
    reasons = [_text(item) for item in reasons if _text(item)] if isinstance(reasons, list) else []
    preflight = _map(payload.get("preflight"))
    skip_reason = _text(payload.get("skip_reason") or preflight.get("skip_reason") or preflight.get("reason"))
    reason = _text(payload.get("reason") or payload.get("error") or skip_reason) or "; ".join(reasons)
    reason_code = _text(payload.get("reason_code") or payload.get("error_code"))
    if exclusion:
        reason_code, reason = exclusion
    reason_code = reason_code or ("preflight_skip" if status == "skip" and skip_reason else {"has_jobs": "raw_jobs_observed", "empty": "empty_raw_result_not_proof_of_no_jobs",
                                  "skip": "skipped", "error": "crawl_error"}[status])
    reason = reason or {"has_jobs": "Raw rows returned; not a success or admission count.",
                        "empty": "No raw rows returned; not proof of no jobs.",
                        "skip": "Entry skipped by capture.", "error": "Capture attempt failed."}[status]

    jd, cohort, urls, missing = Counter(), Counter(), set(), 0
    for index, value in enumerate(jobs):
        job = value if isinstance(value, Mapping) else {}
        jd[_jd_bucket(job)] += 1
        cohort[_cohort_bucket(job)] += 1
        normalized = normalize_job_identity_url(_job_url(job))
        if normalized:
            urls.add(normalized)
            groups[normalized].append({"company": company, "source_key": key, "index": index})
        else:
            missing += 1
    for bucket in JD_BUCKETS:
        jd.setdefault(bucket, 0)
    for bucket in COHORT_BUCKETS:
        cohort.setdefault(bucket, 0)
    advertised = _value(evidence, ("advertised_total", "total", "total_count", "count"), _int)
    observed = raw_count
    pagination = {
        "complete": _value(evidence, ("pagination_complete", "complete"), _bool),
        "known": _value(evidence, ("completeness_known", "known"), _bool),
        "has_more": _value(evidence, ("has_more", "hasMore"), _bool),
        "observed_count": observed, "advertised_count": advertised,
        "count_conflict": advertised is not None and observed != advertised,
        "pages_seen": _value(evidence, ("pages_seen", "pagesSeen"), _int),
        "total_pages": _value(evidence, ("total_pages", "totalPages"), _int),
    }
    pagination["complete_and_known"] = pagination["complete"] is True and pagination["known"] is True
    base.update({
        "checkpointed": True, "status": status, "crawler_status": original or None,
        "reason_code": reason_code, "reason": reason,
        "skip_reason": skip_reason or (exclusion[1] if exclusion else None),
        "crawl_url": _text(payload.get("crawl_url")) or url,
        "raw_job_count": max(raw_count, 0), "raw_jobs_list_count": len(jobs),
        "missing_detail_url_count": missing, "unique_normalized_detail_url_count": len(urls),
        "pagination": pagination, "jd_counts": dict(jd), "cohort_counts": dict(cohort),
        "checkpoint": path.relative_to(checkpoint_dir.parent).as_posix(),
    })
    return base


def _summary_table(entries: list[Mapping[str, Any]], manifest_count: int, groups: Mapping[str, list[dict[str, Any]]],
                  hydration: dict[str, Any] | None, validation: dict[str, Any]) -> dict[str, Any]:
    pre = sum(bool(entry["pre_excluded"]) for entry in entries)
    eligible = manifest_count - pre
    eligible_attempted = sum(bool(entry["network_attempted"]) for entry in entries)
    status = Counter(entry["status"] for entry in entries)
    eligible_status = Counter(entry["status"] for entry in entries if not entry["pre_excluded"])
    raw = sum(int(entry["raw_job_count"]) for entry in entries if not entry["pre_excluded"])
    raw_list = sum(int(entry["raw_jobs_list_count"]) for entry in entries if not entry["pre_excluded"])
    jd = Counter({bucket: 0 for bucket in JD_BUCKETS})
    cohort = Counter({bucket: 0 for bucket in COHORT_BUCKETS})
    page = Counter()
    for entry in entries:
        if entry["pre_excluded"]:
            continue
        jd.update(entry["jd_counts"]); cohort.update(entry["cohort_counts"])
        page["complete_and_known_entries"] += entry["pagination"]["complete_and_known"] is True
        page["has_more_entries"] += entry["pagination"]["has_more"] is True
        page["count_conflict_entries"] += bool(entry["pagination"]["count_conflict"])
        page["has_more_and_count_conflict_entries"] += entry["pagination"]["has_more"] is True and bool(entry["pagination"]["count_conflict"])
    duplicate_groups = [
        {"normalized_detail_url": url, "occurrences": len(rows),
         "companies": sorted({_text(row["company"]) for row in rows}),
         "source_keys": sorted({row["source_key"] for row in rows})}
        for url, rows in sorted(groups.items()) if len(rows) > 1
    ]
    cross = [group for group in duplicate_groups if len(group["companies"]) > 1]
    unique_rows = sum(len(rows) for rows in groups.values())
    return {
        "planned": {"manifest_task_count": manifest_count, "source_entries": len(entries),
                    "pre_excluded": pre, "eligible_to_crawl": eligible,
                    "checkpointed_entries": sum(bool(entry["checkpointed"]) for entry in entries),
                    "pre_excluded_checkpointed": sum(bool(entry["pre_excluded"] and entry["checkpointed"]) for entry in entries),
                    "eligible_attempted": eligible_attempted, "eligible_pending": max(eligible - eligible_attempted, 0),
                    "scope_complete": eligible_attempted >= eligible},
        "attempted": eligible_attempted, "pending": max(eligible - eligible_attempted, 0),
        "entry_status_counts": {key: status.get(key, 0) for key in STATUSES},
        "eligible_status_counts": {key: eligible_status.get(key, 0) for key in STATUSES},
        "raw_jobs": {"raw_returned_rows": raw, "raw_job_rows": raw, "raw_jobs_list_rows": raw_list,
                     "entries_with_raw_jobs": eligible_status.get("has_jobs", 0)},
        "pagination": dict(page), "jd_counts": dict(jd), "cohort_counts": dict(cohort),
        "detail_urls": {"raw_job_rows": raw_list, "unique_normalized_detail_urls": len(groups),
                        "rows_with_detail_url": unique_rows, "missing_detail_url_rows": raw_list - unique_rows,
                        "duplicate_group_count": len(duplicate_groups), "cross_company_group_count": len(cross),
                        "cross_company_occurrence_rows": sum(group["occurrences"] for group in cross),
                        "legal_entity_merge": False, "cross_company_groups": cross},
        "hydration_summary": hydration.get("summary") if hydration else None,
        "hydration": hydration, "validation": validation,
        "entry_status_raw_rows": {key: sum(int(e["raw_job_count"]) for e in entries if e["status"] == key) for key in STATUSES},
    }


def _hydration(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    summary_path = path / "summary.json" if path.is_dir() else path
    result: dict[str, Any] = {"summary_path": str(summary_path.resolve()), "summary": None, "error": None}
    try:
        value = _json(summary_path)
        if not isinstance(value, Mapping):
            raise ValueError("hydration summary must contain an object")
        result["summary"] = dict(value)
    except ValueError as exc:
        result["error"] = str(exc)
    return result


def _cell(value: Any) -> str:
    return " ".join(_text(value).replace("|", "\\|").splitlines())


def _markdown(summary: Mapping[str, Any], entries: list[Mapping[str, Any]]) -> str:
    planned, raw, urls = summary["planned"], summary["raw_jobs"], summary["detail_urls"]
    lines = ["# OfferBiu Full Capture Report", "",
             "Offline report from manifest, sources, and checkpoints; no network, DB, or model calls.", "",
             "## Scope", "", f"- All manifest entries: **{planned['manifest_task_count']}**",
             f"- Pre-excluded by source classification: **{planned['pre_excluded']}**",
             f"- Actual crawl eligible: **{planned['eligible_to_crawl']}**",
             f"- Eligible attempted: **{planned['eligible_attempted']}**",
             f"- Eligible pending: **{planned['eligible_pending']}**", "",
             "Static exclusions are not network attempts or crawl successes. Raw returned rows are not success/admission counts.", "",
             "## Quality", "", f"- Raw returned rows: **{raw['raw_returned_rows']}**; unique normalized detail URLs: **{urls['unique_normalized_detail_urls']}**.",
             f"- JD: `{summary['jd_counts']['complete']} complete`, `{summary['jd_counts']['unknown']} unknown`, `{summary['jd_counts']['incomplete']} incomplete`.",
             f"- Cohort: `{summary['cohort_counts']['2027']} 2027`, `{summary['cohort_counts']['unknown']} unknown`, `{summary['cohort_counts']['other']} other`.",
             f"- Pagination: `{summary['pagination'].get('complete_and_known_entries', 0)} complete+known`, `{summary['pagination'].get('has_more_entries', 0)} has_more`, `{summary['pagination'].get('count_conflict_entries', 0)} count conflicts`.",
             f"- Cross-company normalized URL groups: **{urls['cross_company_group_count']}**; company labels are not legally merged.", "",
             "## Full Entry Table", "", "| # | Company | URL | Source key | Scope | Status | Reason | Observed/advertised | Complete/known | Has more | Count conflict | JD C/U/I | Cohort 2027/U/O |",
             "| ---: | --- | --- | --- | --- | --- | --- | ---: | --- | --- | --- | --- | --- |"]
    for index, entry in enumerate(entries, 1):
        page, jd, cohort = entry["pagination"], entry["jd_counts"], entry["cohort_counts"]
        lines.append("| " + " | ".join((str(index), _cell(entry["company"]), _cell(entry["url"]), _cell(entry["source_key"]),
            "pre_excluded" if entry["pre_excluded"] else "eligible", _cell(entry["status"]), _cell(entry["reason"]),
            f"{page['observed_count']} / {page['advertised_count'] if page['advertised_count'] is not None else '?'}",
            f"{page['complete']} / {page['known']}", _cell(page['has_more']), _cell(page['count_conflict']),
            f"{jd['complete']}/{jd['unknown']}/{jd['incomplete']}", f"{cohort['2027']}/{cohort['unknown']}/{cohort['other']}")) + " |")
    return "\n".join(lines) + "\n"


def run(checkpoint_dir: Path, output_dir: Path, *, manifest_path: Path | None = None,
        sources_path: Path | None = None, hydration_dir: Path | None = None) -> dict[str, Any]:
    checkpoint_dir = Path(checkpoint_dir).resolve()
    if not checkpoint_dir.is_dir():
        raise ValueError(f"Checkpoint directory does not exist: {checkpoint_dir}")
    run_dir = checkpoint_dir.parent if checkpoint_dir.name == "checkpoints" else checkpoint_dir
    output = Path(output_dir).resolve(); eval_root = (ROOT / ".data/evals").resolve()
    if not output.is_relative_to(eval_root):
        raise ValueError("Output must stay under .data/evals")
    if output in {checkpoint_dir, run_dir}:
        raise ValueError("Output must be separate from input state")
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(manifest_path or run_dir / "manifest.json").resolve()
    sources_path = Path(sources_path or run_dir / "sources.json").resolve()
    manifest = _json(manifest_path)
    if not isinstance(manifest, Mapping):
        raise ValueError("manifest.json must contain an object")
    sources = _source_rows(sources_path)
    manifest_count = _int(manifest.get("task_count")) or len(sources)
    keys = {source["source_key"] for source in sources}
    checkpoints, errors, extras = _load_checkpoints(checkpoint_dir, keys)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    entries = [_entry(source, checkpoints.get(source["source_key"]), errors.get(source["source_key"]), checkpoint_dir, groups) for source in sources]
    validation = {"checkpoint_files": len(list(checkpoint_dir.glob("*.json"))), "matched_checkpoint_entries": len(checkpoints),
                  "checkpoint_read_error_entries": len(errors), "extra_checkpoint_files": sorted(extras),
                  "manifest_source_count_mismatch": len(sources) != manifest_count}
    summary = _summary_table(entries, manifest_count, groups, _hydration(hydration_dir), validation)
    summary.update({"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
                    "network_calls": 0, "db_writes": 0, "model_calls": 0, "manifest": dict(manifest),
                    "input": {"run_dir": str(run_dir), "checkpoint_dir": str(checkpoint_dir), "manifest": str(manifest_path), "sources": str(sources_path)}})
    _write_json(output / "quality-summary.json", summary); _write_json(output / "entries.json", entries)
    _write(output / "report.md", _markdown(summary, entries))
    return summary


generate_report = run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-dir", type=Path); group.add_argument("--checkpoint-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True); parser.add_argument("--manifest", type=Path)
    parser.add_argument("--sources", type=Path); parser.add_argument("--hydration-dir", type=Path)
    args = parser.parse_args(argv)
    checkpoint = args.checkpoint_dir or (args.run_dir / "checkpoints" if (args.run_dir / "checkpoints").is_dir() else args.run_dir)
    summary = run(checkpoint, args.output_dir, manifest_path=args.manifest, sources_path=args.sources, hydration_dir=args.hydration_dir)
    print(json.dumps({"all": summary["planned"]["manifest_task_count"], "pre_excluded": summary["planned"]["pre_excluded"],
                      "eligible": summary["planned"]["eligible_to_crawl"], "attempted": summary["attempted"], "pending": summary["pending"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
