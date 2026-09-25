"""Run one company crawler in an isolated child process.

The parent pipeline owns timeouts and process-tree cleanup.  This module keeps
stdout reserved for one JSON response so crawler diagnostics can safely use
stderr without corrupting the protocol.
"""

from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict
import json
import logging
import os
import sys
import threading
from typing import Any, Mapping

import requests

from .models import CompanyConfig
from .runner import crawl_company

logger = logging.getLogger(__name__)


@contextmanager
def _hotjob_list_only(operation: str, company: CompanyConfig):
    if operation != "crawl_company_evidence" or company.crawler != "hotjob":
        yield
        return
    key = "RECRUITOPS_HOTJOB_LIST_ONLY"
    previous = os.environ.get(key)
    os.environ[key] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


@contextmanager
def _company_render_reuse(company: CompanyConfig):
    # These adapters can render consecutive list pages. Moka launches its own
    # browser, so retaining another one here would consume two browser slots.
    if company.crawler not in {"hotjob", "ourpalm"}:
        yield
        return
    from .crawlers.render import company_render_session

    with company_render_session() as session:
        original_send = requests.adapters.HTTPAdapter.send
        owner_thread = threading.get_ident()

        def send(adapter, request, **kwargs):
            # Release the browser lease while the worker switches to HTTP/API
            # work. Playwright must only be closed from its owning thread.
            if threading.get_ident() == owner_thread:
                try:
                    session.close()
                except Exception:
                    logger.warning("browser cleanup failed before HTTP fallback")
            return original_send(adapter, request, **kwargs)

        requests.adapters.HTTPAdapter.send = send
        try:
            yield
        finally:
            requests.adapters.HTTPAdapter.send = original_send


def _read_request() -> Mapping[str, Any]:
    payload = json.load(sys.stdin)
    if not isinstance(payload, Mapping):
        raise ValueError("crawler worker request must be a JSON object")
    return payload


def _execute() -> int:
    from .resources import resource_timing_snapshot

    try:
        request = _read_request()
        operation = str(request.get("operation") or "crawl_company")
        if operation in {"crawl_company", "crawl_company_evidence"}:
            company_payload = request.get("company")
            if not isinstance(company_payload, Mapping):
                raise ValueError("crawler worker request requires a company object")
            company = CompanyConfig.from_legacy(company_payload)
            with redirect_stdout(sys.stderr), _company_render_reuse(company), _hotjob_list_only(operation, company):
                if operation == "crawl_company_evidence":
                    from .entry_crawl import crawl_company_with_entry_discovery

                    result = crawl_company_with_entry_discovery(company)
                    if not isinstance(result, Mapping):
                        raise ValueError("crawler evidence result must be a mapping")
                    if not isinstance(result.get("jobs"), list):
                        raise ValueError("crawler evidence result requires a jobs list")
                    # Crawl failures can accompany useful partial rows. Keep them
                    # inside the result, separate from the worker's protocol status.
                    response = {"ok": True, "result": dict(result)}
                else:
                    jobs = crawl_company(company)
                    response = {"ok": True, "jobs": jobs}
        elif operation == "job_detail":
            job = request.get("job")
            if not isinstance(job, Mapping):
                raise ValueError("job-detail worker request requires a job object")
            from .job_details import fetch_full_job_description_result

            with redirect_stdout(sys.stderr):
                hydration = fetch_full_job_description_result(dict(job))
            response = {
                "ok": True,
                "detail": hydration.detail,
                "hydration": asdict(hydration),
            }
        else:
            raise ValueError(f"unsupported worker operation: {operation}")
        timing = resource_timing_snapshot()
        if any(timing.values()):
            response["resource_timing"] = timing
        print(json.dumps(response, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:  # the parent converts this into a company failure
        response = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        timing = resource_timing_snapshot()
        if any(timing.values()):
            response["resource_timing"] = timing
        print(json.dumps(response, ensure_ascii=False, default=str))
        return 1


def main() -> int:
    from .resources import reset_resource_timings, worker_http_limits

    try:
        reset_resource_timings()
        with worker_http_limits():
            return _execute()
    except Exception as exc:
        print(json.dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
