"""Run one company crawler in an isolated child process.

The parent pipeline owns timeouts and process-tree cleanup.  This module keeps
stdout reserved for one JSON response so crawler diagnostics can safely use
stderr without corrupting the protocol.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import asdict
import json
import sys
from typing import Any, Mapping

from .models import CompanyConfig
from .runner import crawl_company


def _read_request() -> Mapping[str, Any]:
    payload = json.load(sys.stdin)
    if not isinstance(payload, Mapping):
        raise ValueError("crawler worker request must be a JSON object")
    return payload


def main() -> int:
    try:
        request = _read_request()
        operation = str(request.get("operation") or "crawl_company")
        if operation in {"crawl_company", "crawl_company_evidence"}:
            company_payload = request.get("company")
            if not isinstance(company_payload, Mapping):
                raise ValueError("crawler worker request requires a company object")
            company = CompanyConfig.from_legacy(company_payload)
            with redirect_stdout(sys.stderr):
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
        print(json.dumps(response, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:  # the parent converts this into a company failure
        response = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        print(json.dumps(response, ensure_ascii=False, default=str))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
