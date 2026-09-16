from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_daily_scope_eval_uses_mcp_and_forces_external_analysis_off() -> None:
    source = (ROOT / "scripts" / "run_daily_scope_eval.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    assert "session.call_tool" in source
    assert '"daily_recruitment_sync"' in source
    assert '"RECRUITOPS_LLM_ENABLED": "false"' in source
    assert '"RECRUITOPS_JOB_ANALYSIS_ENABLED": "false"' in source
    assert any(
        isinstance(call.func, ast.Attribute) and call.func.attr == "write_text"
        for call in calls
    )


def test_daily_scope_eval_prints_bounded_company_summary(tmp_path: Path) -> None:
    path = ROOT / "scripts" / "run_daily_scope_eval.py"
    spec = importlib.util.spec_from_file_location("run_daily_scope_eval", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = {
        "is_error": False,
        "fixture_version": "test-v1",
        "requested_company_ids": ["co-1"],
        "mcp_tool_count": 26,
        "structured_content": {
            "data": {
                "run_status": "success",
                "result": {
                    "daily_sync": {
                        "status": "succeeded",
                        "pipeline": {
                            "dry_run": True,
                            "total_companies": 1,
                            "selected_companies": 1,
                            "new_job_ids": ["large", "payload"],
                            "companies": [
                                {
                                    "company_id": "co-1",
                                    "company_name": "Company 1",
                                    "status": "complete",
                                    "new_count": 2,
                                    "observed_job_ids": ["large", "payload"],
                                }
                            ],
                        },
                        "offline_reconciliation": {
                            "dry_run": True,
                            "processed_company_count": 1,
                            "plans": [{"large": "payload"}],
                            "written": False,
                        },
                    }
                },
            }
        },
    }

    summary = module._bounded_summary(result, tmp_path / "result.json")

    assert summary["pipeline"]["companies"] == [
        {
            "company_id": "co-1",
            "company_name": "Company 1",
            "status": "complete",
            "raw_job_count": None,
            "accepted_job_count": None,
            "new_count": 2,
            "changed_count": None,
            "reused_count": None,
            "rejected_count": None,
            "filtered_count": None,
            "failure_reason": None,
            "run_reason": None,
        }
    ]
    assert "new_job_ids" not in summary["pipeline"]
    assert "observed_job_ids" not in summary["pipeline"]["companies"][0]
    assert summary["offline_reconciliation"]["processed_company_count"] == 1
    assert "plans" not in summary["offline_reconciliation"]
