from pathlib import Path
import shutil
import subprocess

import pytest

from packages.mcp.server import TOOL_DEFINITIONS
from packages.tools.daily_sync import DailyRecruitmentSyncInput


ROOT = Path(__file__).resolve().parents[1]


def test_full_run_is_unscoped_and_not_a_ten_company_pilot():
    request = DailyRecruitmentSyncInput()
    assert request.mode == "full"
    assert request.dry_run is False
    assert request.company_ids == []
    assert request.source_record_ids == []
    description = next(
        item.description for item in TOOL_DEFINITIONS
        if item.name == "daily_recruitment_sync"
    )
    assert "unscoped full or crawl_only" in description
    assert "only for an explicitly bounded diagnostic run" in description
    assert "frozen scope" in description


def test_agent_guidance_does_not_limit_all_company_discovery_to_pending_entries():
    guidance = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert 'daily_recruitment_sync(mode="full", dry_run=false)' in guidance
    assert "ten is not the full-run" in guidance
    assert "Select readable records from its bounded" not in guidance
    assert "scoring-stage resume reads saved JDs" in guidance


def test_docker_startup_dry_run_does_not_contact_docker():
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is unavailable")
    script = ROOT / "scripts" / "start_docker.ps1"
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(script), "-DryRun"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "DRY-RUN" in result.stdout
    assert "No image build, database reset, volume removal" in result.stdout
