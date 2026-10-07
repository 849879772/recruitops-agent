from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


PROMOTION_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "desktop" / "promote_candidate.ps1"


@pytest.fixture
def run_guard():
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required for promotion guard tests")
    quoted_path = str(PROMOTION_SCRIPT).replace("'", "''")
    # Load definitions via the parser; never run the promotion script's main
    # body, pass -Apply, or access an actual installation directory.
    prelude = rf"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile('{quoted_path}', [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw ($errors -join '; ') }}
foreach ($definition in $ast.FindAll({{ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }}, $false)) {{
    . ([scriptblock]::Create($definition.Extent.Text))
}}
"""

    def run(body: str):
        return subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", prelude + body],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    return run


@pytest.mark.parametrize("exit_code", [0, 10, 259])
def test_only_still_active_blocks_promotion(run_guard, exit_code):
    result = run_guard(
        rf"""
function Get-CimInstance {{
    [pscustomobject]@{{ ProcessId = 123; ExecutablePath = 'C:\guard\desktop.exe'; CommandLine = '' }}
}}
function Get-ProcessExitCode([uint32]$ProcessId) {{
    if ($ProcessId -ne 123) {{ throw 'Unexpected PID' }}
    return [uint32]{exit_code}
}}
Assert-Stopped 'C:\guard'
"""
    )
    if exit_code == 259:
        assert result.returncode != 0
        assert "Formal desktop is still running (1 processes)" in result.stderr
    else:
        assert result.returncode == 0, result.stderr


def test_native_query_failure_blocks_promotion(run_guard):
    result = run_guard(
        r"""
function Get-CimInstance {
    [pscustomobject]@{ ProcessId = 123; ExecutablePath = ''; CommandLine = 'app C:/guard/desktop.exe' }
}
function Get-ProcessExitCode([uint32]$ProcessId) { throw 'Access denied while querying process' }
Assert-Stopped 'C:\guard'
"""
    )
    assert result.returncode != 0
    assert "Access denied while querying process" in result.stderr


def test_unmatched_process_is_not_queried(run_guard):
    result = run_guard(
        r"""
function Get-CimInstance {
    [pscustomobject]@{ ProcessId = 123; ExecutablePath = 'C:\other\desktop.exe'; CommandLine = '' }
}
function Get-ProcessExitCode([uint32]$ProcessId) { throw 'Unmatched process queried' }
Assert-Stopped 'C:\guard'
"""
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Native Windows process query")
def test_native_live_child_blocks_without_stopping_it(run_guard):
    result = run_guard(
        r"""
$childInfo = [Diagnostics.ProcessStartInfo]::new((Get-Process -Id $PID).Path)
$childInfo.UseShellExecute = $false
$childInfo.CreateNoWindow = $true
foreach ($argument in @('-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 5')) {
    $childInfo.ArgumentList.Add($argument)
}
$child = [Diagnostics.Process]::Start($childInfo)
try {
    $script:guardProcessId = $child.Id
    function Get-CimInstance {
        [pscustomobject]@{ ProcessId = $script:guardProcessId; ExecutablePath = 'C:\guard\desktop.exe'; CommandLine = '' }
    }
    if ((Get-ProcessExitCode ([uint32]$child.Id)) -ne 259) { throw 'Child is not active' }
    $blocked = $false
    try { Assert-Stopped 'C:\guard' } catch {
        if ($_.Exception.Message -notlike '*Formal desktop is still running*') { throw }
        $blocked = $true
    }
    if (-not $blocked) { throw 'Active child did not block promotion' }
    if ($child.HasExited) { throw 'Guard stopped the child' }
    if (-not $child.WaitForExit(15000)) { throw 'Child did not finish its own sleep' }
    if ($child.ExitCode -ne 0) { throw 'Child exit code changed' }
} finally {
    $child.Dispose()
}
"""
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Native Windows process query")
def test_native_exited_child_with_retained_handle_does_not_block(run_guard):
    result = run_guard(
        r"""
$childInfo = [Diagnostics.ProcessStartInfo]::new((Get-Process -Id $PID).Path)
$childInfo.UseShellExecute = $false
$childInfo.CreateNoWindow = $true
foreach ($argument in @('-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Milliseconds 250; exit 0')) {
    $childInfo.ArgumentList.Add($argument)
}
$child = [Diagnostics.Process]::Start($childInfo)
try {
    # Retaining a handle reproduces the exited-process object behind stale CIM
    # rows while ensuring a native query can confirm its final exit code.
    $retainedHandle = $child.Handle
    if (-not $child.WaitForExit(15000)) { throw 'Child did not exit' }
    $script:guardProcessId = $child.Id
    function Get-CimInstance {
        [pscustomobject]@{ ProcessId = $script:guardProcessId; ExecutablePath = 'C:\guard\desktop.exe'; CommandLine = '' }
    }
    if ((Get-ProcessExitCode ([uint32]$child.Id)) -ne 0) { throw 'Exit was not confirmed' }
    Assert-Stopped 'C:\guard'
} finally {
    $child.Dispose()
}
"""
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Native Windows process query")
def test_native_unopenable_process_fails_closed(run_guard):
    result = run_guard(
        r"""
function Get-CimInstance {
    [pscustomobject]@{ ProcessId = 0; ExecutablePath = 'C:\guard\desktop.exe'; CommandLine = '' }
}
Assert-Stopped 'C:\guard'
"""
    )
    assert result.returncode != 0
    assert "GetExitCode" in result.stderr


def test_program_removal_keeps_a_guard_immediately_before_each_target(run_guard):
    result = run_guard(
        r"""
$script:events = [Collections.Generic.List[string]]::new()
function Get-ProgramTargets {
    @(
        [pscustomobject]@{ FullName = 'C:\guard\first.exe'; Name = 'first.exe' },
        [pscustomobject]@{ FullName = 'C:\guard\second.exe'; Name = 'second.exe' }
    )
}
function Assert-Stopped { $script:events.Add('check') }
function Assert-Child { }
function Remove-Item { param($LiteralPath, [switch]$Recurse, [switch]$Force) $script:events.Add($LiteralPath) }
Remove-ProgramTargets 'C:\guard' @('first.exe', 'second.exe')
if (($script:events -join ',') -ne 'check,check,C:\guard\first.exe,check,C:\guard\second.exe') {
    throw 'Missing removal guard'
}
"""
    )
    assert result.returncode == 0, result.stderr
