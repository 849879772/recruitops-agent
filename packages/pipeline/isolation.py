"""Hard process boundary for one recruitment crawler invocation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
WORKER_MODULE = "packages.recruitment_core.worker"
CRAWL_TIMEOUT_ENV = "RECRUITOPS_CRAWL_TIMEOUT_SECONDS"
_CRAWL_CLEANUP_RESERVE_SECONDS = 1.0
_RESOURCE_WAIT_ALLOWANCE_SECONDS = 60.0


class IsolatedOperationError(RuntimeError):
    """Raised when an isolated recruitment operation fails its contract."""


class IsolatedOperationTimeout(IsolatedOperationError):
    """Raised after an isolated process tree exceeds its hard timeout."""


class IsolatedWorkerError(IsolatedOperationError):
    """Raised when the worker reports a typed operation failure."""

    def __init__(self, message: str, *, error_type: str | None = None,
                 resource_timing: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.resource_timing = dict(resource_timing or {})


IsolatedCrawlerError = IsolatedOperationError
IsolatedCrawlerTimeout = IsolatedOperationTimeout


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            # Always attempt the tree cleanup. A worker may have exited while a
            # Playwright browser process is still shutting down.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
    else:  # pragma: no cover - Windows is the production host
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive fallback
        process.kill()


def _run_isolated(
    request: Mapping[str, Any],
    *,
    timeout_seconds: float,
    label: str,
    python_executable: str | None = None,
    resource_root: Path | str | None = None,
    browser_max_concurrency: int = 6,
) -> Mapping[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("operation timeout must be positive")
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    if type(browser_max_concurrency) is not int or not 1 <= browser_max_concurrency <= 6:
        raise ValueError("browser_max_concurrency must be between 1 and 6")
    if resource_root is not None:
        environment["RECRUITOPS_CRAWL_RESOURCE_ROOT"] = str(Path(resource_root).resolve())
    environment["RECRUITOPS_BROWSER_MAX_CONCURRENCY"] = str(browser_max_concurrency)
    # A worker may wait for a shared browser/host slot before performing any
    # network work. Bound that wait independently from the crawl work budget.
    hard_timeout = timeout_seconds + _RESOURCE_WAIT_ALLOWANCE_SECONDS
    environment["RECRUITOPS_CRAWL_RESOURCE_DEADLINE"] = str(time.monotonic() + hard_timeout)
    if request.get("operation") in {"crawl_company", "crawl_company_evidence"}:
        # Keep adapter-level waits inside the parent deadline and leave a small
        # margin for JSON transport and process-tree cleanup.
        reserve = min(_CRAWL_CLEANUP_RESERVE_SECONDS, timeout_seconds * 0.1)
        child_budget = max(0.001, timeout_seconds - reserve)
        environment[CRAWL_TIMEOUT_ENV] = f"{child_budget:.6f}"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    encoded_request = json.dumps(request, ensure_ascii=False, default=str)
    process = subprocess.Popen(
        [python_executable or sys.executable, "-m", WORKER_MODULE],
        cwd=str(ROOT),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    cleanup_tree = False
    try:
        stdout, stderr = process.communicate(encoded_request, timeout=hard_timeout)
    except subprocess.TimeoutExpired as exc:
        cleanup_tree = True
        raise IsolatedOperationTimeout(
            f"{label} exceeded hard timeout of {hard_timeout:g}s"
        ) from exc
    except BaseException:
        cleanup_tree = True
        raise
    finally:
        # communicate normally reaps a successful worker. Interrupted calls
        # need explicit process-tree cleanup for Playwright descendants.
        if cleanup_tree:
            _terminate_process_tree(process)

    try:
        payload = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        detail = stderr.strip()[-1_000:]
        raise IsolatedOperationError(
            f"{label} worker returned invalid JSON (exit={process.returncode}): {detail}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise IsolatedOperationError(f"{label} worker response must be a JSON object")
    if process.returncode != 0 or payload.get("ok") is not True:
        detail = str(payload.get("error") or stderr.strip() or "unknown worker error")
        error_type = str(payload.get("error_type") or "").strip() or None
        timing = payload.get("resource_timing")
        raise IsolatedWorkerError(
            detail[-2_000:], error_type=error_type,
            resource_timing=timing if isinstance(timing, Mapping) else None,
        )
    return payload


def crawl_company_isolated(
    company: Mapping[str, Any],
    *,
    timeout_seconds: float,
    python_executable: str | None = None,
    resource_root: Path | str | None = None,
    browser_max_concurrency: int = 6,
) -> Sequence[Any]:
    """Execute one crawler in a disposable child process."""

    payload = _run_isolated(
        {"operation": "crawl_company", "company": dict(company)},
        timeout_seconds=timeout_seconds,
        label="crawler",
        python_executable=python_executable,
        resource_root=resource_root,
        browser_max_concurrency=browser_max_concurrency,
    )
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise IsolatedOperationError("crawler worker response requires a jobs list")
    return jobs


def crawl_company_result_isolated(
    company: Mapping[str, Any],
    *,
    timeout_seconds: float,
    python_executable: str | None = None,
    resource_root: Path | str | None = None,
    browser_max_concurrency: int = 6,
) -> dict[str, Any]:
    """Crawl with entry discovery and retain partial rows and unmodified evidence.

    Missing evidence stays missing; explicit false, null, and zero values are
    never replaced with inferred completeness or counts.
    """

    payload = _run_isolated(
        {"operation": "crawl_company_evidence", "company": dict(company)},
        timeout_seconds=timeout_seconds,
        label="crawler evidence",
        python_executable=python_executable,
        resource_root=resource_root,
        browser_max_concurrency=browser_max_concurrency,
    )
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise IsolatedOperationError("crawler evidence worker response requires a result object")
    jobs = result.get("jobs")
    if not isinstance(jobs, list) or any(not isinstance(job, Mapping) for job in jobs):
        raise IsolatedOperationError("crawler evidence result requires a jobs list of objects")

    for field in ("termination_reasons", "effective_source_urls", "source_runs", "failures"):
        if field not in result:
            continue
        values = result[field]
        item_type = (
            Mapping if field == "source_runs"
            else (str, Mapping) if field == "failures"
            else str
        )
        if not isinstance(values, list) or any(not isinstance(value, item_type) for value in values):
            raise IsolatedOperationError(f"crawler evidence {field} must be a list of valid entries")

    evidence_records = [("result", result)] + [
        (f"source_runs[{index}]", run)
        for index, run in enumerate(result.get("source_runs", []))
    ]
    for location, evidence in evidence_records:
        for field in ("pagination_complete", "completeness_known", "has_more"):
            value = evidence.get(field)
            if value is not None and type(value) is not bool:
                raise IsolatedOperationError(
                    f"crawler evidence {location}.{field} must be a boolean or null"
                )
        for field in ("raw_job_count", "pages_seen", "total_pages", "advertised_total"):
            value = evidence.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise IsolatedOperationError(
                    f"crawler evidence {location}.{field} must be a non-negative integer or null"
                )
        for field in ("source_url", "discovered_entry_url", "error_code"):
            value = evidence.get(field)
            if value is not None and not isinstance(value, str):
                raise IsolatedOperationError(
                    f"crawler evidence {location}.{field} must be a string or null"
                )

    returned = dict(result)
    timing = payload.get("resource_timing")
    if isinstance(timing, Mapping):
        returned["resource_timing"] = dict(timing)
    return returned


def fetch_job_detail_isolated(
    job: Mapping[str, Any],
    *,
    timeout_seconds: float,
    python_executable: str | None = None,
    resource_root: Path | str | None = None,
    browser_max_concurrency: int = 6,
) -> str:
    """Hydrate one JD in a disposable child process using the legacy text API."""

    return str(
        fetch_job_detail_result_isolated(
            job,
            timeout_seconds=timeout_seconds,
            python_executable=python_executable,
            resource_root=resource_root,
            browser_max_concurrency=browser_max_concurrency,
        ).get("detail")
        or ""
    )


def fetch_job_detail_result_isolated(
    job: Mapping[str, Any],
    *,
    timeout_seconds: float,
    python_executable: str | None = None,
    resource_root: Path | str | None = None,
    browser_max_concurrency: int = 6,
) -> dict[str, Any]:
    """Hydrate one JD and retain the worker's structured diagnostics."""

    payload = _run_isolated(
        {"operation": "job_detail", "job": dict(job)},
        timeout_seconds=timeout_seconds,
        label="job detail",
        python_executable=python_executable,
        resource_root=resource_root,
        browser_max_concurrency=browser_max_concurrency,
    )
    hydration = payload.get("hydration")
    if isinstance(hydration, Mapping):
        result = dict(hydration)
        result["detail"] = str(result.get("detail") or "")
        result["status"] = str(result.get("status") or "fetch_failed")
        attempts = result.get("attempts")
        result["attempts"] = list(attempts) if isinstance(attempts, (list, tuple)) else []
        timing = payload.get("resource_timing")
        if isinstance(timing, Mapping):
            result["resource_timing"] = dict(timing)
        return result

    # Accept responses from older workers while deployments are rolling over.
    detail = str(payload.get("detail") or "")
    return {
        "detail": detail,
        "status": "complete" if detail else "content_incomplete",
        "source": "legacy_worker",
        "detail_url": str(job.get("detail_url") or job.get("jd_url") or ""),
        "attempts": [],
        "error_type": "",
    }


__all__ = [
    "IsolatedCrawlerError",
    "IsolatedCrawlerTimeout",
    "IsolatedOperationError",
    "IsolatedOperationTimeout",
    "IsolatedWorkerError",
    "crawl_company_isolated",
    "crawl_company_result_isolated",
    "fetch_job_detail_isolated",
    "fetch_job_detail_result_isolated",
]
