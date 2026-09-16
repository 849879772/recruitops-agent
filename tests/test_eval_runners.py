from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from evals.mvp_runner import run_frozen_mvp
from evals.rag_runner import run_frozen_rag


ROOT = Path(__file__).resolve().parents[1]


def test_frozen_mvp_uses_current_task_surface_and_all_cases_pass() -> None:
    results = run_frozen_mvp()

    assert len(results) == 5
    assert all(item.passed for item in results)
    assert results[-1].scenario == "recruitment_mail_detail"


def test_frozen_rag_contains_only_crawler_knowledge_and_passes() -> None:
    summary = run_frozen_rag()

    assert summary.cases == 4
    assert summary.mean_recall_at_k == 1.0
    assert summary.mean_citation_precision == 1.0
    assert summary.refusal_accuracy == 1.0


def test_mvp_cli_completes_successfully() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "evals.mvp_runner"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert "recruitment_mail_detail" in completed.stdout
