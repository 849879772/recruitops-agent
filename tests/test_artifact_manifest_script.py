from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_artifact_manifest_check_runs_as_a_direct_script() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/build_artifact_manifest.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
