from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
REGRESSION_SCRIPT = ROOT / "tests" / "frontend" / "workbench_stream.test.js"


def test_workbench_stream_regression_suite() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the deterministic frontend stream regression suite"

    result = subprocess.run(
        [node, "--test", str(REGRESSION_SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"frontend regression failed:\n{result.stdout}\n{result.stderr}"
