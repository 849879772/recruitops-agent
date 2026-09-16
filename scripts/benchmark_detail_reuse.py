"""Offline, deterministic acceptance benchmark for ReusingDetailHydrator."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from time import perf_counter
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import hydrate_offerbiu_full_crawl as capture

RUN = ROOT / ".data" / "evals" / "offerbiu_full_20260908_v1"
RAW = RUN / "raw-jobs.jsonl"
CHECKPOINTS = RUN / "hydration" / "checkpoints"
SUMMARY = RUN / "hydration" / "summary.json"
OUTPUT_STEM = ROOT / ".data" / "evals" / "detail-reuse-benchmark-20260909"
EXPLICIT_NATIVE_FIELDS = capture._EXPLICIT_NATIVE_ID_FIELDS
TENANT_FIELDS = ("tenant_id", "tenantId", "source_tenant_id", "sourceTenantId", "tenant", "source_tenant")
SUCCESS = {"complete", "hydrated"}
FAILURE = {"failed", "fetch_failed", "timeout", "error", "content_incomplete", "official_unavailable", "official_sparse"}


class OfflineAccessError(RuntimeError):
    pass


def text(value: object) -> str:
    return "" if value is None else str(value).strip()


def detail_hash(value: object) -> str:
    value = text(value)
    return hashlib.sha256(value.encode()).hexdigest() if value else ""


def normalize_url(value: object) -> str:
    return capture.normalize_job_identity_url(text(value))


def official_url(value: object) -> tuple[str, str, str] | None:
    try:
        original = urlsplit(text(value))
        if (
            original.scheme.casefold() != "https"
            or original.username
            or original.password
            or original.port not in (None, 443)
            or (original.hostname or "").startswith("www.")
        ):
            return None
    except ValueError:
        return None
    normalized = normalize_url(value)
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    host, parts = (parsed.hostname or "").casefold(), [p for p in parsed.path.split("/") if p]
    if host == "jobs.bytedance.com" and len(parts) == 4 and parts[:2] == ["campus", "position"] and parts[3] == "detail":
        return normalized, "bytedance", parts[2]
    if host.endswith(".jobs.feishu.cn"):
        try:
            index = parts.index("position")
        except ValueError:
            return None
        if index + 2 < len(parts) and parts[index + 1] and parts[index + 2] == "detail":
            return normalized, "feishu", parts[index + 1]
    return None


def job_key(company: str, job: Mapping[str, object]) -> str:
    return capture.job_identity(company, job)["job_key"]


def input_hash(company: str, crawl_url: str, job: Mapping[str, object]) -> str:
    return capture._sha256({"company": company, "crawl_url": crawl_url, "job": dict(job)})


def confirmed(job: Mapping[str, object]) -> bool:
    try:
        return int(job.get("cohort") or 0) == 2027 and text(job.get("cohort_status")).casefold() == "confirmed"
    except (TypeError, ValueError):
        return False


def evidence_values(value: Mapping[str, object], prefix: str) -> list[str]:
    values = value.get("identity_evidence")
    if not isinstance(values, (list, tuple)):
        return []
    return [text(item).split(":", 1)[1].strip() for item in values if isinstance(item, str) and item.startswith(prefix + ":")]


def row(line: int, value: Mapping[str, object]) -> dict[str, object] | None:
    job = value.get("job")
    if not isinstance(job, Mapping) or not confirmed(job):
        return None
    parsed = official_url(job.get("detail_url") or job.get("jd_url"))
    if parsed is None:
        return None
    url, platform, _ = parsed
    company = text(value.get("company_label")) or text(value.get("company")) or text(job.get("source")) or text(job.get("company"))
    if not company:
        return None
    job = dict(job)
    return {
        "line": line,
        "company": company,
        "source_label": text(value.get("company_label")) or text(job.get("source")) or company,
        "crawl_url": text(value.get("crawl_url")),
        "title": text(job.get("title")),
        "detail_url": text(job.get("detail_url") or job.get("jd_url")),
        "url": url,
        "platform": platform,
        "job": job,
        "job_key": job_key(company, job),
        "input_hash": input_hash(company, text(value.get("crawl_url")), job),
    }


def select_groups(path: Path, limit: int = 5, rows_per_group: int = 9) -> tuple[list[dict[str, object]], dict[str, int]]:
    if not 1 <= limit <= 5 or not 2 <= rows_per_group <= 9:
        raise ValueError("Replay requires 1-5 groups with 2-9 source rows per group")
    groups: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    scanned = candidates = 0
    with path.open(encoding="utf-8") as handle:
        for line, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            scanned += 1
            value = json.loads(raw)
            if not isinstance(value, Mapping):
                continue
            item = row(line, value)
            if item is not None:
                candidates += 1
                groups[str(item["url"])].append(item)
    selected = []
    for url, items in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        items.sort(key=lambda item: (str(item["source_label"]), int(item["line"])))
        if len(items) < 2 or len({str(item["source_label"]) for item in items}) < 2:
            continue
        bounded = items[:rows_per_group]
        selected.append({"group_key": url, "platform": bounded[0]["platform"], "detail_url": bounded[0]["detail_url"], "rows": bounded, "available": len(items)})
        if len(selected) == limit:
            break
    return selected, {"scanned_rows": scanned, "confirmed_official_rows": candidates, "selected_groups": len(selected), "group_limit": limit, "rows_per_group": rows_per_group}


def evidence_ok(item: Mapping[str, object], hydration: Mapping[str, object]) -> tuple[str, str]:
    status = text(hydration.get("status")).casefold()
    if status in FAILURE and hydration.get("request_made", True) is not False:
        return "qualified_failure", "terminal failure fixture"
    evidence = hydration.get("capture_evidence")
    parsed = official_url(item["detail_url"])
    checks = [
        status in SUCCESS and bool(text(hydration.get("detail"))),
        text(hydration.get("identity_status")).casefold() in {"matched", "request_bound"},
        isinstance(evidence, Mapping) and text(evidence.get("method")).casefold() == "official_api",
        isinstance(evidence, Mapping) and official_url(evidence.get("source_url")) == parsed,
        isinstance(evidence, Mapping) and text(evidence.get("status")).casefold() == "complete",
        isinstance(evidence, Mapping) and evidence.get("identity_verified") is True and evidence.get("terminal_observed") is True and evidence.get("remaining_controls") in ([], None),
        isinstance(evidence, Mapping) and text(evidence.get("content_sha256")) == detail_hash(hydration.get("detail")),
        parsed is not None and {parsed[2]} == set(evidence_values(hydration, "native_id")),
        {item["title"]} == set(evidence_values(hydration, "title")),
        not any(text(v).split(":", 1)[0].casefold() in {"company", "employer", "organization"} for v in hydration.get("identity_evidence", []) if isinstance(v, str) and ":" in v),
    ]
    return ("qualified_success", "verified native ID/title and official capture") if all(checks) else ("unverified", "missing or weak official identity/capture evidence")


def load_fixture(item: Mapping[str, object], checkpoint_dir: Path) -> tuple[dict[str, object] | None, str, str, Path | None]:
    path = checkpoint_dir / f"{item['job_key']}.json"
    if not path.is_file():
        return None, "unverified", "checkpoint not found by job_key", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, "unverified", f"checkpoint read error: {type(exc).__name__}", path
    hydration = payload.get("hydration") if isinstance(payload, Mapping) else None
    if not isinstance(payload, Mapping) or payload.get("job_key") != item["job_key"] or payload.get("input_sha256") != item["input_hash"] or not isinstance(hydration, Mapping):
        return None, "unverified", "checkpoint identity/input/hydration mismatch", path
    kind, reason = evidence_ok(item, hydration)
    return dict(hydration), kind, reason, path


def scope_issues(group: Mapping[str, object]) -> list[str]:
    rows = group["rows"]
    titles = {text(item["title"]) for item in rows}
    issues = []
    if len(titles) != 1 or "" in titles:
        issues.append("source rows disagree on title")
    tenant_hints = set()
    for item in rows:
        job = item["job"]
        urls = [normalize_url(job.get(field)) for field in ("detail_url", "jd_url") if text(job.get(field))]
        if any(url != group["group_key"] for url in urls) or len(set(urls)) != 1:
            issues.append(f"scope URL mismatch at line {item['line']}")
        parsed = official_url(item["detail_url"])
        explicit = {text(job.get(field)) for field in EXPLICIT_NATIVE_FIELDS if text(job.get(field))}
        if parsed is None or (explicit and explicit != {parsed[2]}):
            issues.append(f"native ID mismatch at line {item['line']}")
        hints = {text(job.get(field)).casefold() for field in TENANT_FIELDS if text(job.get(field))}
        if len(hints) > 1:
            issues.append(f"tenant scope conflict at line {item['line']}")
        tenant_hints.update(hints)
    if len(tenant_hints) > 1:
        issues.append("source rows disagree on tenant scope")
    return sorted(set(issues))


class FixtureFetch:
    def __init__(self, fixtures: Mapping[str, dict[str, object]]):
        self.fixtures, self.calls = fixtures, 0
        self.by_key: Counter[str] = Counter()

    def __call__(self, job: Mapping[str, object], *, timeout_seconds: float) -> dict[str, object]:
        del timeout_seconds
        key = text(job.get("__benchmark_job_key"))
        if key not in self.fixtures:
            raise RuntimeError("fixture fetch requested an unknown source job")
        self.calls += 1
        self.by_key[key] += 1
        return deepcopy(self.fixtures[key])


def status_counts(results: Sequence[Mapping[str, object]]) -> dict[str, object]:
    values = Counter("success" if text(item.get("status")).casefold() in SUCCESS else "failure" for item in results)
    return {"rows": len(results), "success": values["success"], "failure": values["failure"], "status_counts": dict(Counter(text(item.get("status")) or "missing" for item in results))}


def identity_ok(item: Mapping[str, object], result: Mapping[str, object]) -> bool:
    if text(result.get("status")).casefold() not in SUCCESS:
        return True
    return evidence_ok(item, result)[0] == "qualified_success"


def source_ok(item: Mapping[str, object], result: Mapping[str, object]) -> bool:
    for key in ("company", "company_label"):
        if text(result.get(key)):
            return text(result.get(key)) == item["source_label"]
    return True


@contextmanager
def offline_guard() -> Iterator[None]:
    def deny(*_: object, **__: object) -> None:
        raise OfflineAccessError("network/DB access is forbidden")
    old_connect, old_connect_ex, old_create, old_sqlite = socket.socket.connect, socket.socket.connect_ex, socket.create_connection, sqlite3.connect
    socket.socket.connect, socket.socket.connect_ex, socket.create_connection, sqlite3.connect = deny, deny, deny, deny  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.create_connection, sqlite3.connect = old_connect, old_connect_ex, old_create, old_sqlite  # type: ignore[assignment]


def load_hydrator():
    module = importlib.import_module("packages.recruitment_core.detail_reuse")
    return module.ReusingDetailHydrator


def run_reuse(cls, items: Sequence[Mapping[str, object]], fixtures: Mapping[str, dict[str, object]], timeout: float) -> tuple[list[dict[str, object]], FixtureFetch, dict[str, object] | None]:
    fetch = FixtureFetch(fixtures)
    started = perf_counter()
    hydrator = cls(fetch)
    results = []
    for item in items:
        job = dict(item["job"])
        job["company_label"] = item["source_label"]
        job["__benchmark_job_key"] = item["job_key"]
        value = hydrator(job, timeout_seconds=timeout)
        if not isinstance(value, Mapping):
            raise TypeError("ReusingDetailHydrator result must be a mapping")
        results.append(dict(value))
    elapsed = perf_counter() - started
    snapshot = None
    method = getattr(hydrator, "snapshot", None)
    if callable(method):
        value = method()
        snapshot = dict(value) if isinstance(value, Mapping) else {"value": repr(value)}
    close = getattr(hydrator, "close", None)
    if callable(close):
        close()
    return results, fetch, {"wall_seconds": elapsed, "snapshot": snapshot}


def worker_startup() -> dict[str, object]:
    code = "import socket, sqlite3; deny=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('network/DB forbidden')); socket.socket.connect=deny; socket.socket.connect_ex=deny; socket.create_connection=deny; sqlite3.connect=deny; from packages.recruitment_core import worker, job_details; print('imports-ok')"
    started = perf_counter()
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "RECRUITOPS_BENCHMARK_NO_NETWORK": "1", "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, check=False, timeout=30)
    return {"enabled": True, "runs": 1, "wall_seconds": perf_counter() - started, "status": "ok" if result.returncode == 0 and "imports-ok" in result.stdout else "failed", "stderr": result.stderr[-500:]}


def run(raw: Path = RAW, checkpoints: Path = CHECKPOINTS, summary: Path = SUMMARY, *, groups: int = 5, rows_per_group: int = 9, hydrator_cls=None, timeout: float = 45.0, measure_startup: bool = False) -> dict[str, object]:
    selected, selection = select_groups(raw, groups, rows_per_group)
    summary_snapshot = json.loads(summary.read_text(encoding="utf-8"))
    if not isinstance(summary_snapshot, Mapping):
        raise ValueError("hydration summary must be an object")
    loaded: dict[str, tuple[dict[str, object] | None, str, str, Path | None]] = {}
    replay_fixtures: dict[str, dict[str, object]] = {}
    report_groups = []
    verified = []
    for group in selected:
        for item in group["rows"]:
            loaded[str(item["job_key"])] = load_fixture(item, checkpoints)
        reasons = scope_issues(group)
        representatives = [item for item in group["rows"] if loaded[str(item["job_key"])][1] == "qualified_success"]
        if not representatives:
            reasons.append("no qualified success response fixture")
        hashes = {detail_hash(loaded[str(item["job_key"])][0].get("detail")) for item in representatives if loaded[str(item["job_key"])][0] is not None}
        if len(hashes) > 1:
            reasons.append("qualified representative fixtures disagree on body hash")
        representative = representatives[0] if representatives else None
        representative_key = str(representative["job_key"]) if representative else None
        representative_path = loaded[representative_key][3] if representative_key else None
        payload = {"group_key": group["group_key"], "platform": group["platform"], "detail_url": group["detail_url"], "row_count": len(group["rows"]), "available_row_count": group["available"], "scope_validated": not scope_issues(group), "verification": "verified" if not reasons else "unverified", "unverified_reasons": sorted(set(reasons)), "replay_fixture_job_key": representative_key, "replay_fixture_path": str(representative_path) if representative_path else None, "rows": [{"line": item["line"], "job_key": item["job_key"], "source_label": item["source_label"], "fixture_class": loaded[str(item["job_key"])][1], "fixture_path": str(loaded[str(item["job_key"])][3]) if loaded[str(item["job_key"])][3] else None, "replay_fixture_job_key": representative_key} for item in group["rows"]]}
        report_groups.append(payload)
        if not reasons:
            for item in group["rows"]:
                verified.append(item)
                replay_fixtures[str(item["job_key"])] = deepcopy(loaded[representative_key][0])
    fixtures = replay_fixtures
    baseline_fetch = FixtureFetch(fixtures)
    started = perf_counter()
    baseline = []
    for item in verified:
        request = dict(item["job"], __benchmark_job_key=item["job_key"])
        baseline.append(baseline_fetch(request, timeout_seconds=timeout))
    baseline_wall = perf_counter() - started
    reuse: dict[str, object] = {"status": "not_run" if not verified else "blocked", "fetch_calls": 0, "wall_seconds": 0.0, "results": status_counts([]), "snapshot": None, "fixture_replay": True, "public_speedup_claim": False}
    comparisons = []
    if verified:
        try:
            with offline_guard():
                cls = hydrator_cls or load_hydrator()
                new, fetch, timing = run_reuse(cls, verified, fixtures, timeout)
        except ModuleNotFoundError as exc:
            reuse["reason"] = f"detail_reuse unavailable: {exc}"
        else:
            reuse.update({"status": "ok", "fetch_calls": fetch.calls, "calls_by_job_key": dict(fetch.by_key), **timing, "results": status_counts(new)})
            for item, old, current in zip(verified, baseline, new):
                comparisons.append({"job_key": item["job_key"], "source_label": item["source_label"], "baseline_hash": detail_hash(old.get("detail")), "new_hash": detail_hash(current.get("detail")), "hash_equal": detail_hash(old.get("detail")) == detail_hash(current.get("detail")), "identity_consistent": identity_ok(item, current), "source_preserved": source_ok(item, current), "status_equal": (text(old.get("status")).casefold() in SUCCESS) == (text(current.get("status")).casefold() in SUCCESS), "detail_reuse": current.get("_detail_reuse", {})})
    comparison_summary = {"rows": len(comparisons), "hash_equal": sum(item["hash_equal"] for item in comparisons), "identity_consistent": sum(item["identity_consistent"] for item in comparisons), "source_preserved": sum(item["source_preserved"] for item in comparisons), "matched": sum(item["hash_equal"] and item["identity_consistent"] and item["source_preserved"] and item["status_equal"] for item in comparisons)}
    return {"schema_version": 1, "read_only": True, "network": "forbidden", "db_writes": 0, "model_calls": 0, "fixture_replay": {"is_public_speedup": False, "wall_seconds_note": "Fixture replay only; wall_seconds is not public-site or production end-to-end latency.", "sleep_injected": False}, "inputs": {"raw_jobs": str(raw.resolve()), "hydration_checkpoints": str(checkpoints.resolve()), "hydration_summary": str(summary.resolve()), "hydration_summary_snapshot": dict(summary_snapshot)}, "selection": selection, "groups": report_groups, "verified_group_count": sum(group["verification"] == "verified" for group in report_groups), "unverified_group_count": sum(group["verification"] == "unverified" for group in report_groups), "baseline": {"fetch_calls": baseline_fetch.calls, "wall_seconds": baseline_wall, "results": status_counts(baseline)}, "reuse": reuse, "request_count_reduction": baseline_fetch.calls - int(reuse["fetch_calls"]) if reuse.get("status") == "ok" else None, "comparisons": comparisons, "comparison_summary": comparison_summary, "worker_startup": worker_startup() if measure_startup else {"enabled": False}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-jobs", type=Path, default=RAW)
    parser.add_argument("--checkpoint-dir", type=Path, default=CHECKPOINTS)
    parser.add_argument("--hydration-summary", type=Path, default=SUMMARY)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--groups", type=int, default=5)
    parser.add_argument("--rows-per-group", type=int, default=9)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--measure-worker-startup", action="store_true")
    args = parser.parse_args(argv)
    report = run(args.raw_jobs, args.checkpoint_dir, args.hydration_summary, groups=args.groups, rows_per_group=args.rows_per_group, timeout=args.timeout, measure_startup=args.measure_worker_startup)
    output = (args.output_dir or OUTPUT_STEM).resolve()
    if not output.is_relative_to((ROOT / ".data" / "evals").resolve()) or output.exists():
        raise SystemExit("refusing output outside .data/evals or existing output directory")
    output.mkdir(parents=True)
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output), "verified_groups": report["verified_group_count"], "unverified_groups": report["unverified_group_count"], "baseline_fetch_calls": report["baseline"]["fetch_calls"], "reuse_status": report["reuse"]["status"], "reuse_fetch_calls": report["reuse"]["fetch_calls"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
