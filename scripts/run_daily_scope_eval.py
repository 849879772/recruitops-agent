"""Run the frozen bounded daily-sync fixture through the real MCP stdio boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "evals" / "fixtures" / "daily_scope_10_20260906.json"
DEFAULT_OUTPUT = ROOT / ".data" / "evals" / "daily_scope_10_20260906.json"


def _bounded_summary(result: dict[str, object], output: Path) -> dict[str, object]:
    structured = result.get("structured_content") or {}
    data = structured.get("data") if isinstance(structured, dict) else None
    operation_result = data.get("result") if isinstance(data, dict) else None
    daily_sync = (
        operation_result.get("daily_sync")
        if isinstance(operation_result, dict)
        else None
    )
    pipeline = daily_sync.get("pipeline") if isinstance(daily_sync, dict) else None
    companies = pipeline.get("companies") if isinstance(pipeline, dict) else None
    company_summaries = []
    if isinstance(companies, list):
        for company in companies:
            if not isinstance(company, dict):
                continue
            company_summaries.append(
                {
                    key: company.get(key)
                    for key in (
                        "company_id",
                        "company_name",
                        "status",
                        "raw_job_count",
                        "accepted_job_count",
                        "new_count",
                        "changed_count",
                        "reused_count",
                        "rejected_count",
                        "filtered_count",
                        "failure_reason",
                        "run_reason",
                    )
                }
            )
    pipeline_summary = None
    if isinstance(pipeline, dict):
        pipeline_summary = {
            "dry_run": pipeline.get("dry_run"),
            "total_companies": pipeline.get("total_companies"),
            "selected_companies": pipeline.get("selected_companies"),
            "crawled_companies": pipeline.get("crawled_companies"),
            "failed_company_count": pipeline.get("failed_companies"),
            "new_count": pipeline.get("new"),
            "changed_count": pipeline.get("changed"),
            "reused_count": pipeline.get("reused"),
            "rejected_count": pipeline.get("rejected"),
            "filtered_count": pipeline.get("filtered"),
            "written": pipeline.get("written"),
            "scoped_company_ids": pipeline.get("scoped_company_ids"),
            "failure_reasons": pipeline.get("failure_reasons"),
        }
        pipeline_summary["companies"] = company_summaries
    offline = daily_sync.get("offline_reconciliation") if isinstance(daily_sync, dict) else None
    offline_summary = None
    if isinstance(offline, dict):
        offline_summary = {
            key: offline.get(key)
            for key in (
                "dry_run",
                "processed_company_count",
                "skipped_company_count",
                "observed_count",
                "missing_count",
                "inactive_count",
                "restored_count",
                "planned_only_count",
                "written",
                "skipped_company_ids",
            )
        }
    return {
        "output": str(output),
        "is_error": bool(result.get("is_error")),
        "fixture_version": result.get("fixture_version"),
        "requested_company_count": len(result["requested_company_ids"]),
        "mcp_tool_count": result.get("mcp_tool_count"),
        "run_status": data.get("run_status") if isinstance(data, dict) else None,
        "sync_status": daily_sync.get("status") if isinstance(daily_sync, dict) else None,
        "pipeline": pipeline_summary,
        "offline_reconciliation": offline_summary,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


async def _run(fixture: Path) -> dict[str, object]:
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    company_ids = [str(row["id"]) for row in payload["companies"]]
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "RECRUITOPS_LLM_ENABLED": "false",
            "RECRUITOPS_JOB_ANALYSIS_ENABLED": "false",
            "RECRUITOPS_OC_BROWSER_CAPTURE_ENABLED": "false",
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "scripts" / "run_mcp_server.py"), "--profile", "agent"],
        env=environment,
        cwd=ROOT,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = [tool.name for tool in tools.tools]
            if "daily_recruitment_sync" not in tool_names:
                raise RuntimeError("daily_recruitment_sync is missing from the Agent MCP profile")
            result = await session.call_tool(
                "daily_recruitment_sync",
                {
                    "request": {
                        "dry_run": True,
                        "company_ids": company_ids,
                        "timeout_ms": 120_000,
                    }
                },
                read_timeout_seconds=21_600,
            )
            dumped = result.model_dump(mode="json")
            if dumped.get("is_error"):
                text = " ".join(
                    str(item.get("text") or "")
                    for item in dumped.get("content") or []
                    if isinstance(item, dict)
                )
                raise RuntimeError(text or "daily_recruitment_sync MCP call failed")
            dumped["fixture_version"] = payload["fixture_version"]
            dumped["requested_company_ids"] = company_ids
            dumped["mcp_tool_count"] = len(tool_names)
            return dumped


def main() -> None:
    args = _arguments()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(_run(args.fixture.expanduser().resolve()))
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = _bounded_summary(result, output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
