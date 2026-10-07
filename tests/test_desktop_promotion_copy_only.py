from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


PROMOTION_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "desktop" / "promote_candidate.ps1"


def ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def make_tree(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def result_json(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def run_copy_only():
    if os.name != "nt":
        pytest.skip("Desktop promotion paths are Windows paths")
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is required for promotion copy-only tests")
    # Parse and load definitions only. Never run the script's main body, pass
    # -Apply, query native processes, or access an installation directory.
    prelude = rf"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile({ps_quote(PROMOTION_SCRIPT)}, [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw ($errors -join '; ') }}
foreach ($definition in $ast.FindAll({{ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }}, $false)) {{
    . ([scriptblock]::Create($definition.Extent.Text))
}}
foreach ($name in @('Get-CopyOnlyChanges', 'Assert-ProgramBackup', 'Copy-CopyOnlyChanges', 'Restore-CopyOnlyChanges', 'Invoke-CopyOnlyPromotion')) {{
    if (-not (Get-Command -Name $name -CommandType Function -ErrorAction SilentlyContinue)) {{ throw "Missing function: $name" }}
}}
$script:events = [Collections.Generic.List[string]]::new()
$script:copyDestination = ''
$script:copyNumber = 0
$script:writeFailureAt = 0
function Assert-Stopped([string]$Root) {{
    if ($Root -ne $script:copyDestination) {{ throw 'Unexpected guard root' }}
    $script:events.Add('check')
}}
function Copy-Item {{
    [CmdletBinding()]
    param([string]$LiteralPath, [string]$Destination, [switch]$Force)
    $script:copyNumber++
    $relative = [IO.Path]::GetRelativePath($script:copyDestination, $Destination)
    $script:events.Add('write:' + $relative)
    if ($script:copyNumber -eq $script:writeFailureAt) {{ throw 'Injected failure before write' }}
    Microsoft.PowerShell.Management\Copy-Item @PSBoundParameters
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


@pytest.mark.parametrize(
    ("candidate_names", "formal_names"),
    [
        (["keep.bin", "added.bin"], ["keep.bin"]),
        (["keep.bin"], ["keep.bin", "removed.bin"]),
        (["keep.bin", "candidate-only.bin"], ["keep.bin", "formal-only.bin"]),
    ],
)
def test_copy_only_requires_exact_file_paths(run_copy_only, tmp_path, candidate_names, formal_names):
    candidate = make_tree(tmp_path / "candidate", {name: b"same" for name in candidate_names})
    formal = make_tree(tmp_path / "formal", {name: b"same" for name in formal_names})
    result = run_copy_only(f"Get-CopyOnlyChanges {ps_quote(candidate)} {ps_quote(formal)}")
    assert result.returncode != 0
    assert "identical program file paths" in result.stderr
    assert sorted(path.name for path in formal.iterdir()) == sorted(formal_names)


def test_candidate_root_data_is_rejected(run_copy_only, tmp_path):
    candidate = make_tree(tmp_path / "candidate", {"app.bin": b"new", ".data/private.bin": b"private"})
    formal = make_tree(tmp_path / "formal", {"app.bin": b"old"})
    result = run_copy_only(f"Get-CopyOnlyChanges {ps_quote(candidate)} {ps_quote(formal)}")
    assert result.returncode != 0
    assert "contains user data" in result.stderr
    assert (formal / "app.bin").read_bytes() == b"old"


def test_backup_digest_rejects_tampered_program(run_copy_only, tmp_path):
    backup = make_tree(tmp_path / "program", {"app.bin": b"old", "resources/native.dll": b"native"})
    output = result_json(run_copy_only(rf"""
$program = {ps_quote(backup)}
$digest = (Get-TreeSummary $program -ContentHashes).Digest
Assert-ProgramBackup $program $digest
[IO.File]::WriteAllText((Join-Path $program 'app.bin'), 'tampered')
$message = ''
try {{ Assert-ProgramBackup $program $digest }} catch {{ $message = $_.Exception.Message }}
[PSCustomObject]@{{ Message = $message; Events = @($script:events) }} | ConvertTo-Json -Compress
"""))
    assert "backup hash mismatch" in output["Message"]
    assert output["Events"] == []


def test_backup_root_data_is_rejected_even_with_matching_digest(run_copy_only, tmp_path):
    backup = make_tree(tmp_path / "program", {"app.bin": b"old", ".data/private.bin": b"private"})
    result = run_copy_only(rf"""
$program = {ps_quote(backup)}
Assert-ProgramBackup $program (Get-TreeSummary $program -ContentHashes).Digest
""")
    assert result.returncode != 0
    assert "backup unexpectedly contains user data" in result.stderr


@pytest.mark.parametrize("exists_as_file", [False, True])
def test_backup_must_be_a_directory(run_copy_only, tmp_path, exists_as_file):
    backup = tmp_path / "program"
    if exists_as_file:
        backup.write_bytes(b"not a directory")
    result = run_copy_only(f"Assert-ProgramBackup {ps_quote(backup)} 'irrelevant'")
    assert result.returncode != 0
    assert "Program backup missing" in result.stderr


def test_same_bytes_with_different_mtimes_are_never_written(run_copy_only, tmp_path):
    candidate = make_tree(tmp_path / "candidate", {"native.dll": b"same bytes"})
    formal = make_tree(tmp_path / "formal", {"native.dll": b"same bytes", ".data/state.bin": b"user data"})
    os.utime(candidate / "native.dll", ns=(1_000_000_000_000_000_000,) * 2)
    os.utime(formal / "native.dll", ns=(1_500_000_000_000_000_000,) * 2)
    before = (formal / "native.dll").stat().st_mtime_ns
    output = result_json(run_copy_only(rf"""
$source = {ps_quote(candidate)}
$script:copyDestination = {ps_quote(formal)}
$changes = @(Get-CopyOnlyChanges $source $script:copyDestination)
$attempted = [Collections.Generic.List[object]]::new()
Copy-CopyOnlyChanges $source $script:copyDestination $changes $attempted
[PSCustomObject]@{{ Changes = @($changes); Attempted = $attempted.Count; Events = @($script:events) }} | ConvertTo-Json -Depth 4 -Compress
"""))
    assert output == {"Changes": [], "Attempted": 0, "Events": []}
    assert (formal / "native.dll").stat().st_mtime_ns == before
    assert (formal / ".data/state.bin").read_bytes() == b"user data"


def test_only_changed_bytes_are_copied_with_a_guard_for_every_write(run_copy_only, tmp_path):
    candidate_files = {"app.bin": b"new app", "resources/new.bin": b"new resource", "native.dll": b"native"}
    formal_files = {"app.bin": b"old app", "resources/new.bin": b"old resource", "native.dll": b"native"}
    candidate = make_tree(tmp_path / "candidate", candidate_files)
    formal = make_tree(tmp_path / "formal", {**formal_files, ".data/state.bin": b"user data"})
    native_before = (formal / "native.dll").stat().st_mtime_ns
    data_before = (formal / ".data/state.bin").stat().st_mtime_ns
    output = result_json(run_copy_only(rf"""
$source = {ps_quote(candidate)}
$script:copyDestination = {ps_quote(formal)}
$changes = @(Get-CopyOnlyChanges $source $script:copyDestination)
$attempted = [Collections.Generic.List[object]]::new()
Copy-CopyOnlyChanges $source $script:copyDestination $changes $attempted
[PSCustomObject]@{{ Changes = @($changes); Attempted = $attempted.Count; Events = @($script:events) }} | ConvertTo-Json -Depth 4 -Compress
"""))
    changes = {change["RelativePath"].replace("\\", "/"): change for change in output["Changes"]}
    assert set(changes) == {"app.bin", "resources/new.bin"}
    for relative, change in changes.items():
        assert change["OldHash"] == hashlib.sha256(formal_files[relative]).hexdigest().upper()
        assert change["NewHash"] == hashlib.sha256(candidate_files[relative]).hexdigest().upper()
    assert output["Attempted"] == 2
    assert len(output["Events"]) == 4
    assert output["Events"][::2] == ["check", "check"]
    assert {event.removeprefix("write:").replace("\\", "/") for event in output["Events"][1::2]} == set(changes)
    for relative, content in candidate_files.items():
        assert (formal / relative).read_bytes() == content
    assert (formal / "native.dll").stat().st_mtime_ns == native_before
    assert (formal / ".data/state.bin").stat().st_mtime_ns == data_before
    assert (formal / ".data/state.bin").read_bytes() == b"user data"


@pytest.mark.parametrize("mutated_root", ["candidate", "formal"])
def test_changed_file_since_planning_rejects_before_attempting_copy(run_copy_only, tmp_path, mutated_root):
    candidate = make_tree(tmp_path / "candidate", {"app.bin": b"new"})
    formal = make_tree(tmp_path / "formal", {"app.bin": b"old"})
    mutated = candidate if mutated_root == "candidate" else formal
    output = result_json(run_copy_only(rf"""
$source = {ps_quote(candidate)}
$script:copyDestination = {ps_quote(formal)}
$changes = @(Get-CopyOnlyChanges $source $script:copyDestination)
[IO.File]::WriteAllText({ps_quote(mutated / 'app.bin')}, 'changed after planning')
$attempted = [Collections.Generic.List[object]]::new()
$message = ''
try {{ Copy-CopyOnlyChanges $source $script:copyDestination $changes $attempted }} catch {{ $message = $_.Exception.Message }}
[PSCustomObject]@{{ Message = $message; Attempted = $attempted.Count; Events = @($script:events) }} | ConvertTo-Json -Compress
"""))
    assert "program file changed" in output["Message"]
    assert output["Attempted"] == 0
    assert not any(event.startswith("write:") for event in output["Events"])
    assert (formal / "app.bin").read_bytes() == (b"old" if mutated_root == "candidate" else b"changed after planning")


def test_partial_copy_failure_rolls_back_only_attempted_changed_files(run_copy_only, tmp_path):
    old_files = {"first.bin": b"old first", "second.bin": b"old second", "native.dll": b"native"}
    candidate = make_tree(tmp_path / "candidate", {**old_files, "first.bin": b"new first", "second.bin": b"new second"})
    formal = make_tree(tmp_path / "formal", {**old_files, ".data/state.bin": b"user data"})
    backup = make_tree(tmp_path / "program", old_files)
    native_before = (formal / "native.dll").stat().st_mtime_ns
    output = result_json(run_copy_only(rf"""
$source = {ps_quote(candidate)}
$script:copyDestination = {ps_quote(formal)}
$backup = {ps_quote(backup)}
$changes = @(Get-CopyOnlyChanges $source $script:copyDestination)
$attempted = [Collections.Generic.List[object]]::new()
$script:writeFailureAt = 2
$message = ''
try {{ Copy-CopyOnlyChanges $source $script:copyDestination $changes $attempted }} catch {{ $message = $_.Exception.Message }}
$copyEvents = @($script:events)
$script:events.Clear()
$script:writeFailureAt = 0
Restore-CopyOnlyChanges $backup $script:copyDestination @($attempted.ToArray())
$rollbackEvents = @($script:events)
$script:events.Clear()
Restore-CopyOnlyChanges $backup $script:copyDestination @($attempted.ToArray())
[PSCustomObject]@{{ Message = $message; Attempted = $attempted.Count; CopyEvents = $copyEvents; RollbackEvents = $rollbackEvents; SecondRollbackEvents = @($script:events) }} | ConvertTo-Json -Depth 4 -Compress
"""))
    assert output["Message"] == "Injected failure before write"
    assert output["Attempted"] == 2
    assert output["CopyEvents"] == ["check", "write:first.bin", "check", "write:second.bin"]
    # The failed second copy still has its old hash and must be skipped during
    # rollback, which would otherwise retry the same locked native file.
    assert output["RollbackEvents"] == ["check", "write:first.bin"]
    assert output["SecondRollbackEvents"] == []
    for relative, content in old_files.items():
        assert (formal / relative).read_bytes() == content
    assert (formal / "native.dll").stat().st_mtime_ns == native_before
    assert (formal / ".data/state.bin").read_bytes() == b"user data"


@pytest.mark.parametrize("relative", [r"..\outside.bin", r".data\state.bin"])
def test_copy_rejects_forged_escape_or_data_path(run_copy_only, tmp_path, relative):
    candidate = make_tree(tmp_path / "candidate", {"app.bin": b"new", ".data/state.bin": b"new data"})
    formal = make_tree(tmp_path / "formal", {"app.bin": b"old", ".data/state.bin": b"old data"})
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    output = result_json(run_copy_only(rf"""
$source = {ps_quote(candidate)}
$script:copyDestination = {ps_quote(formal)}
$change = [PSCustomObject]@{{ RelativePath = {ps_quote(relative)}; OldHash = 'unused'; NewHash = 'unused' }}
$attempted = [Collections.Generic.List[object]]::new()
$message = ''
try {{ Copy-CopyOnlyChanges $source $script:copyDestination @($change) $attempted }} catch {{ $message = $_.Exception.Message }}
[PSCustomObject]@{{ Message = $message; Attempted = $attempted.Count; Events = @($script:events) }} | ConvertTo-Json -Compress
"""))
    assert "copy target" in output["Message"] or "escapes validated directory" in output["Message"]
    assert output["Attempted"] == 0
    assert output["Events"] == []
    assert outside.read_bytes() == b"outside"
    assert (formal / ".data/state.bin").read_bytes() == b"old data"


def test_linked_program_root_is_rejected_for_plan_and_backup(run_copy_only, tmp_path):
    candidate = make_tree(tmp_path / "candidate", {"app.bin": b"new"})
    formal = make_tree(tmp_path / "formal", {"app.bin": b"old"})
    link = tmp_path / "candidate-link"
    output = result_json(run_copy_only(rf"""
$source = {ps_quote(candidate)}
$link = {ps_quote(link)}
New-Item -ItemType Junction -Path $link -Target $source | Out-Null
$digest = (Get-TreeSummary $source -ContentHashes).Digest
$planMessage = ''
$backupMessage = ''
try {{ Get-CopyOnlyChanges $link {ps_quote(formal)} }} catch {{ $planMessage = $_.Exception.Message }}
try {{ Assert-ProgramBackup $link $digest }} catch {{ $backupMessage = $_.Exception.Message }}
[PSCustomObject]@{{ PlanMessage = $planMessage; BackupMessage = $backupMessage; Events = @($script:events) }} | ConvertTo-Json -Compress
"""))
    assert "Linked path rejected" in output["PlanMessage"]
    assert "Linked path rejected" in output["BackupMessage"]
    assert output["Events"] == []
    assert (formal / "app.bin").read_bytes() == b"old"


def test_copy_only_default_preview_reports_counts_without_copying(run_copy_only, tmp_path):
    old_files = {"app.bin": b"old", "resources/resource.bin": b"same", "native.dll": b"native"}
    candidate = make_tree(tmp_path / "candidate", {**old_files, "app.bin": b"new"})
    formal = make_tree(tmp_path / "formal", {**old_files, ".data/state.bin": b"user data"})
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    make_tree(backup_root / "program", old_files)
    before = {relative: (formal / relative).stat().st_mtime_ns for relative in old_files}
    output = result_json(run_copy_only(rf"""
$source = {ps_quote(candidate)}
$formal = {ps_quote(formal)}
$beforeData = Get-TreeSummary (Join-Path $formal '.data')
$expected = Get-TreeSummary $source -ContentHashes
$old = Get-TreeSummary $formal -ContentHashes -SkipData
$preview = Invoke-CopyOnlyPromotion $source $formal {ps_quote(backup_root)} $beforeData $expected $old -ReuseBackup | ConvertFrom-Json
[PSCustomObject]@{{ Apply = $preview.Apply; CopyOnly = $preview.CopyOnly; ReuseBackup = $preview.ReuseBackup; ChangedFiles = $preview.ChangedFiles; UnchangedFiles = $preview.UnchangedFiles; Changes = @($preview.Changes); Events = @($script:events) }} | ConvertTo-Json -Depth 4 -Compress
"""))
    assert output["Apply"] is False
    assert output["CopyOnly"] is True
    assert output["ReuseBackup"] is True
    assert output["ChangedFiles"] == 1
    assert output["UnchangedFiles"] == 2
    assert [change["RelativePath"] for change in output["Changes"]] == ["app.bin"]
    assert output["Events"] == []
    for relative, content in old_files.items():
        assert (formal / relative).read_bytes() == content
        assert (formal / relative).stat().st_mtime_ns == before[relative]
    assert (formal / ".data/state.bin").read_bytes() == b"user data"
    assert not (backup_root / "promotion-report.json").exists()


def test_copy_only_default_preview_rejects_bad_reused_backup(run_copy_only, tmp_path):
    candidate = make_tree(tmp_path / "candidate", {"app.bin": b"new"})
    formal = make_tree(tmp_path / "formal", {"app.bin": b"old", ".data/state.bin": b"user data"})
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    make_tree(backup_root / "program", {"app.bin": b"tampered backup"})
    output = result_json(run_copy_only(rf"""
$source = {ps_quote(candidate)}
$formal = {ps_quote(formal)}
$beforeData = Get-TreeSummary (Join-Path $formal '.data')
$expected = Get-TreeSummary $source -ContentHashes
$old = Get-TreeSummary $formal -ContentHashes -SkipData
$message = ''
try {{ Invoke-CopyOnlyPromotion $source $formal {ps_quote(backup_root)} $beforeData $expected $old -ReuseBackup }} catch {{ $message = $_.Exception.Message }}
[PSCustomObject]@{{ Message = $message; Events = @($script:events) }} | ConvertTo-Json -Compress
"""))
    assert "backup hash mismatch" in output["Message"]
    assert output["Events"] == []
    assert (formal / "app.bin").read_bytes() == b"old"
    assert (formal / ".data/state.bin").read_bytes() == b"user data"
    assert not (backup_root / "promotion-report.json").exists()


def test_copy_only_default_preview_rejects_linked_report_path(run_copy_only, tmp_path):
    candidate = make_tree(tmp_path / "candidate", {"app.bin": b"new"})
    formal = make_tree(tmp_path / "formal", {"app.bin": b"old", ".data/state.bin": b"user data"})
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    make_tree(backup_root / "program", {"app.bin": b"old"})
    data_before = (formal / ".data/state.bin").stat().st_mtime_ns
    output = result_json(run_copy_only(rf"""
$source = {ps_quote(candidate)}
$formal = {ps_quote(formal)}
$backup = {ps_quote(backup_root)}
New-Item -ItemType Junction -Path (Join-Path $backup 'promotion-report.json') -Target (Join-Path $formal '.data') | Out-Null
$beforeData = Get-TreeSummary (Join-Path $formal '.data')
$expected = Get-TreeSummary $source -ContentHashes
$old = Get-TreeSummary $formal -ContentHashes -SkipData
$message = ''
try {{ Invoke-CopyOnlyPromotion $source $formal $backup $beforeData $expected $old -ReuseBackup }} catch {{ $message = $_.Exception.Message }}
[PSCustomObject]@{{ Message = $message; Events = @($script:events) }} | ConvertTo-Json -Compress
"""))
    assert "Linked path rejected" in output["Message"]
    assert output["Events"] == []
    assert (formal / "app.bin").read_bytes() == b"old"
    assert (formal / ".data/state.bin").read_bytes() == b"user data"
    assert (formal / ".data/state.bin").stat().st_mtime_ns == data_before
    assert sorted(path.name for path in (formal / ".data").iterdir()) == ["state.bin"]
