param(
    [Parameter(Mandatory)][string]$Candidate,
    [Parameter(Mandatory)][string]$Backup,
    [string]$Repository = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [switch]$Apply,
    [switch]$CopyOnly,
    [switch]$ReuseBackup
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-PlainPath([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
                throw "Linked path rejected: $cursor"
            }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
}

function Assert-Child([string]$Parent, [string]$Child) {
    if (-not [IO.Path]::GetFullPath($Child).StartsWith($Parent.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Path escapes validated directory'
    }
    Assert-PlainPath $Child
}

function Assert-TreeEntry([string]$Root, [IO.FileSystemInfo]$Entry) {
    # The root and its ancestors are checked once before enumerating. Inspect
    # every child (including directories) without re-reading all its ancestors.
    if (-not [IO.Path]::GetFullPath($Entry.FullName).StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Enumerated path escapes validated directory'
    }
    if ($Entry.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { throw "Linked path rejected: $($Entry.FullName)" }
}

function Get-TreeSummary([string]$Root, [switch]$ContentHashes, [switch]$SkipData) {
    if ($ContentHashes) { Assert-PlainPath $Root }
    $rows = [Collections.Generic.List[string]]::new()
    [long]$bytes = 0
    [long]$files = 0
    $roots = @(Get-ChildItem -LiteralPath $Root -Force | Where-Object { -not $SkipData -or $_.Name -ne '.data' })
    foreach ($item in $roots) {
        if ($ContentHashes) { Assert-TreeEntry $Root $item }
        $entries = @($item)
        if ($item.PSIsContainer -and -not $item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            $entries += @(Get-ChildItem -LiteralPath $item.FullName -Recurse -Force)
        }
        foreach ($entry in $entries) {
            $relative = [IO.Path]::GetRelativePath($Root, $entry.FullName)
            if ($ContentHashes) { Assert-TreeEntry $Root $entry }
            if ($entry.PSIsContainer) { if (-not $ContentHashes) { $rows.Add('dir:' + $relative) }; continue }
            $files++; $bytes += $entry.Length
            if ($ContentHashes) {
                $rows.Add($relative + ':' + $entry.Length + ':' + (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash)
            } else {
                $rows.Add($relative + ':' + $entry.Length + ':' + $entry.LastWriteTimeUtc.Ticks)
            }
        }
    }
    $rows.Sort([StringComparer]::Ordinal)
    $digest = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes(($rows -join "`n"))))
    return [PSCustomObject]@{ Files = $files; Bytes = $bytes; Digest = $digest }
}

function Get-ProcessExitCode([uint32]$ProcessId) {
    if (-not ('RecruitOps.Deployment.ProcessQuery' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace RecruitOps.Deployment {
    public static class ProcessQuery {
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, uint processId);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetExitCodeProcess(IntPtr process, out uint exitCode);
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);

        public static uint GetExitCode(uint processId) {
            IntPtr process = OpenProcess(0x1000, false, processId);
            if (process == IntPtr.Zero) { throw new Win32Exception(Marshal.GetLastWin32Error()); }
            try {
                uint exitCode;
                if (!GetExitCodeProcess(process, out exitCode)) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                return exitCode;
            } finally {
                CloseHandle(process);
            }
        }
    }
}
'@
    }
    return [RecruitOps.Deployment.ProcessQuery]::GetExitCode($ProcessId)
}

function Assert-Stopped([string]$Root) {
    $matched = @(Get-CimInstance Win32_Process | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath.Replace('/', '\').StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.CommandLine -and $_.CommandLine.Replace('/', '\').IndexOf($Root + '\', [StringComparison]::OrdinalIgnoreCase) -ge 0)
    })
    # CIM can retain an exited process while another process holds its handle.
    # Ignore only a confirmed exit; native query failures must block promotion.
    $running = @($matched | Where-Object { (Get-ProcessExitCode ([uint32]$_.ProcessId)) -eq 259 })
    if ($running.Count) { throw "Formal desktop is still running ($($running.Count) processes); exit normally first" }
}

function Get-ProgramTargets([string]$Root, [string[]]$AllowedNames) {
    Assert-PlainPath $Root
    $targets = @(Get-ChildItem -LiteralPath $Root -Force | Where-Object Name -ne '.data')
    # Validate the complete target set before allowing any deletion. Never return
    # the installation root, .data, or a recursively discovered deletion target.
    foreach ($item in $targets) {
        Assert-TreeEntry $Root $item
        if ([IO.Path]::GetDirectoryName($item.FullName) -ne $Root -or $item.Name -notin $AllowedNames) {
            throw "Unexpected program target; no removal permitted: $($item.Name)"
        }
        if ($item.PSIsContainer) {
            foreach ($entry in @(Get-ChildItem -LiteralPath $item.FullName -Force -Recurse)) {
                Assert-TreeEntry $Root $entry
            }
        }
    }
    return $targets
}

function Remove-ProgramTargets([string]$Root, [string[]]$AllowedNames) {
    $targets = @(Get-ProgramTargets $Root $AllowedNames)
    Assert-Stopped $Root
    foreach ($item in $targets) {
        # Recheck the direct target immediately before each operation; the entire
        # recursive tree was already checked above for links and path escapes.
        Assert-Stopped $Root
        Assert-Child $Root $item.FullName
        if ([IO.Path]::GetDirectoryName($item.FullName) -ne $Root -or $item.Name -eq '.data') {
            throw 'Unsafe program removal target'
        }
        Remove-Item -LiteralPath $item.FullName -Recurse -Force
    }
}

function Copy-ProgramTree([string]$Source, [string]$Destination, [string]$ExcludedData = '') {
    $copyArguments = @($Source, $Destination, '/E', '/COPY:DAT', '/DCOPY:DAT', '/R:1', '/W:1', '/XJ', '/NFL', '/NDL', '/NP')
    # Exclude .data on every copy, including candidate and rollback copies, even
    # if an external launch unexpectedly creates it after the initial preflight.
    $copyArguments += @('/XD', (Join-Path $Source '.data'))
    if ($ExcludedData -and $ExcludedData -ne (Join-Path $Source '.data')) { $copyArguments += $ExcludedData }
    & robocopy @copyArguments
    if ($LASTEXITCODE -ge 8) { throw "Program copy failed with robocopy exit code $LASTEXITCODE" }
}

function Get-ProgramFileHashes([string]$Root, [switch]$SkipData) {
    Assert-PlainPath $Root
    if (-not $SkipData -and (Test-Path -LiteralPath (Join-Path $Root '.data'))) { throw 'Program tree contains user data' }
    $hashes = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
    foreach ($item in @(Get-ChildItem -LiteralPath $Root -Force | Where-Object { -not $SkipData -or $_.Name -ne '.data' })) {
        Assert-TreeEntry $Root $item
        $entries = @($item)
        if ($item.PSIsContainer) { $entries += @(Get-ChildItem -LiteralPath $item.FullName -Force -Recurse) }
        foreach ($entry in $entries) {
            Assert-TreeEntry $Root $entry
            if (-not $entry.PSIsContainer) {
                $hashes.Add([IO.Path]::GetRelativePath($Root, $entry.FullName), (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash)
            }
        }
    }
    return ,$hashes
}

function Get-CopyOnlyChanges([string]$Source, [string]$Destination) {
    $sourceHashes = Get-ProgramFileHashes $Source
    $oldHashes = Get-ProgramFileHashes $Destination -SkipData
    if ($sourceHashes.Count -ne $oldHashes.Count) { throw 'Copy-only requires identical program file paths' }
    foreach ($relative in $oldHashes.Keys) {
        if (-not $sourceHashes.ContainsKey($relative)) { throw 'Copy-only requires identical program file paths' }
    }
    foreach ($relative in @($oldHashes.Keys | Sort-Object -CaseSensitive)) {
        if ($oldHashes[$relative] -ne $sourceHashes[$relative]) {
            [PSCustomObject]@{ RelativePath = $relative; OldHash = $oldHashes[$relative]; NewHash = $sourceHashes[$relative] }
        }
    }
}

function Assert-ProgramBackup([string]$Program, [string]$ExpectedDigest) {
    Assert-PlainPath $Program
    if (-not (Test-Path -LiteralPath $Program -PathType Container)) { throw 'Program backup missing' }
    if (Test-Path -LiteralPath (Join-Path $Program '.data')) { throw 'Program backup unexpectedly contains user data' }
    if ((Get-TreeSummary $Program -ContentHashes).Digest -ne $ExpectedDigest) { throw 'Old program backup hash mismatch' }
}

function Get-VerifiedProgramCopyPaths([string]$Source, [string]$Destination, [string]$RelativePath, [string]$CurrentHash, [string]$SourceHash) {
    $relative = $RelativePath.Replace('/', '\')
    if ([IO.Path]::IsPathRooted($relative) -or $relative -eq '.data' -or $relative.StartsWith('.data\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Unsafe program copy target'
    }
    $sourceFile = Join-Path $Source $relative
    $targetFile = Join-Path $Destination $relative
    Assert-Child $Source $sourceFile
    Assert-Child $Destination $targetFile
    if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf) -or -not (Test-Path -LiteralPath $targetFile -PathType Leaf)) {
        throw 'Copy-only file missing'
    }
    Assert-Stopped $Destination
    if ((Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash -ne $SourceHash) { throw "Source program file changed: $relative" }
    if ((Get-FileHash -LiteralPath $targetFile -Algorithm SHA256).Hash -ne $CurrentHash) { throw "Current program file changed: $relative" }
    return [PSCustomObject]@{ Source = $sourceFile; Destination = $targetFile }
}

function Copy-CopyOnlyChanges([string]$Source, [string]$Destination, [object[]]$Changes, [Collections.Generic.List[object]]$AttemptedChanges) {
    foreach ($change in $Changes) {
        $paths = Get-VerifiedProgramCopyPaths $Source $Destination $change.RelativePath $change.OldHash $change.NewHash
        # Include a copy that fails partway so rollback can restore that file.
        $AttemptedChanges.Add($change)
        Copy-Item -LiteralPath $paths.Source -Destination $paths.Destination -Force
        if ((Get-FileHash -LiteralPath $paths.Destination -Algorithm SHA256).Hash -ne $change.NewHash) { throw "Copied file hash mismatch: $($change.RelativePath)" }
    }
}

function Restore-CopyOnlyChanges([string]$BackupProgram, [string]$Destination, [object[]]$Changes) {
    for ($index = $Changes.Count - 1; $index -ge 0; $index--) {
        $change = $Changes[$index]
        $targetFile = Join-Path $Destination $change.RelativePath
        Assert-Child $Destination $targetFile
        $currentHash = (Get-FileHash -LiteralPath $targetFile -Algorithm SHA256).Hash
        if ($currentHash -eq $change.OldHash) { continue }
        $paths = Get-VerifiedProgramCopyPaths $BackupProgram $Destination $change.RelativePath $currentHash $change.OldHash
        Copy-Item -LiteralPath $paths.Source -Destination $paths.Destination -Force
        if ((Get-FileHash -LiteralPath $paths.Destination -Algorithm SHA256).Hash -ne $change.OldHash) { throw "Restored file hash mismatch: $($change.RelativePath)" }
    }
}

function Invoke-CopyOnlyPromotion([string]$CandidateRoot, [string]$Formal, [string]$BackupRoot, [object]$BeforeData, [object]$Expected, [object]$Old, [switch]$ReuseBackup, [switch]$Apply) {
    $oldProgram = Join-Path $BackupRoot 'program'
    $reportPath = Join-Path $BackupRoot 'promotion-report.json'
    Assert-Child $BackupRoot $reportPath
    $data = Join-Path $Formal '.data'
    $changes = @(Get-CopyOnlyChanges $CandidateRoot $Formal)
    if ($ReuseBackup) { Assert-ProgramBackup $oldProgram $Old.Digest }
    if (-not $Apply) {
        [PSCustomObject]@{
            Apply = $false; CopyOnly = $true; ReuseBackup = [bool]$ReuseBackup
            Formal = $Formal; Candidate = $CandidateRoot; Backup = $oldProgram
            Data = $BeforeData; CandidateFiles = $Expected.Files; IdenticalFilePaths = $true
            OldProgramDigest = $Old.Digest; CandidateProgramDigest = $Expected.Digest
            ChangedFiles = $changes.Count; UnchangedFiles = $Expected.Files - $changes.Count
            Changes = $changes
        } | ConvertTo-Json -Depth 5
        return
    }
    $attempted = [Collections.Generic.List[object]]::new()
    try {
        Assert-Stopped $Formal
        if (-not $ReuseBackup) {
            New-Item -ItemType Directory -Path $BackupRoot | Out-Null
            New-Item -ItemType Directory -Path $oldProgram | Out-Null
            Copy-ProgramTree $Formal $oldProgram $data
        }
        Assert-ProgramBackup $oldProgram $Old.Digest
        Assert-Stopped $Formal
        if ((Get-TreeSummary $Formal -ContentHashes -SkipData).Digest -ne $Old.Digest) { throw 'Formal program changed while preparing its backup' }
        if ((Get-TreeSummary $CandidateRoot -ContentHashes).Digest -ne $Expected.Digest) { throw 'Candidate changed before promotion' }
        if ((Get-TreeSummary $data).Digest -ne $BeforeData.Digest) { throw 'Data inventory changed before promotion' }
        Copy-CopyOnlyChanges $CandidateRoot $Formal $changes $attempted
        $actual = Get-TreeSummary $Formal -ContentHashes -SkipData
        if ($actual.Digest -ne $Expected.Digest) { throw 'Formal files differ from candidate' }
        $afterData = Get-TreeSummary $data
        if ($afterData.Digest -ne $BeforeData.Digest) { throw 'Data inventory changed during promotion' }
        Assert-Stopped $Formal
        $report = [PSCustomObject]@{
            Formal = $Formal; Candidate = $CandidateRoot; Backup = $oldProgram
            CopyOnly = $true; ReuseBackup = [bool]$ReuseBackup; IdenticalFilePaths = $true
            ChangedFiles = $changes.Count; UnchangedFiles = $Expected.Files - $changes.Count; Changes = $changes
            OldProgramDigest = $Old.Digest; CandidateProgramDigest = $Expected.Digest
            ProgramFiles = $actual.Files; ProgramBytes = $actual.Bytes; ProgramDigest = $actual.Digest
            DataUnchanged = $true; DataFiles = $afterData.Files; DataBytes = $afterData.Bytes
            DataInventoryDigest = $afterData.Digest; DesktopStarted = $false
            CompletedUtc = [DateTime]::UtcNow.ToString('o')
        }
        Assert-Child $BackupRoot $reportPath
        $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding utf8
        $report | ConvertTo-Json -Depth 5
    } catch {
        $failure = $_
        if ($attempted.Count) {
            try {
                Assert-Stopped $Formal
                Assert-ProgramBackup $oldProgram $Old.Digest
                Restore-CopyOnlyChanges $oldProgram $Formal @($attempted.ToArray())
                if ((Get-TreeSummary $Formal -ContentHashes -SkipData).Digest -ne $Old.Digest) { throw 'Restored program hash mismatch' }
                if ((Get-TreeSummary $data).Digest -ne $BeforeData.Digest) { throw 'Data inventory changed during rollback' }
                Assert-Stopped $Formal
            } catch {
                throw "Promotion failed ($($failure.Exception.Message)); automatic rollback stopped ($($_.Exception.Message)). Program backup retained at: $oldProgram. User data was not moved or deleted."
            }
        }
        throw $failure
    }
}

if ($ReuseBackup -and -not $CopyOnly) { throw 'ReuseBackup requires CopyOnly' }
$repo = (Resolve-Path -LiteralPath $Repository).Path
$formal = [IO.Path]::GetFullPath((Join-Path $repo 'artifacts/releases/RecruitOps'))
$candidateRoot = (Resolve-Path -LiteralPath $Candidate).Path
$backupRoot = [IO.Path]::GetFullPath($Backup)
foreach ($path in @($formal, $candidateRoot, $backupRoot)) { Assert-PlainPath $path }
if (-not (Test-Path -LiteralPath $formal -PathType Container)) { throw 'Formal directory missing' }
if ((Split-Path $candidateRoot -Leaf) -ne 'recruitops-desktop-win32-x64' -or (Split-Path (Split-Path $candidateRoot -Parent) -Leaf) -notmatch '^n-[a-f0-9]{8}$') {
    throw 'Expected a fresh native build candidate'
}
if (Test-Path -LiteralPath $backupRoot) {
    if (-not ($CopyOnly -and $ReuseBackup)) { throw 'Backup must be a fresh directory' }
} elseif ($ReuseBackup) { throw 'Backup to reuse is missing' }
foreach ($left in @($formal, $candidateRoot, $backupRoot)) {
    foreach ($right in @($formal, $candidateRoot, $backupRoot)) {
        if ($left -ne $right -and $right.StartsWith($left + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Candidate, formal and backup must not overlap' }
    }
}
if ($candidateRoot -eq $formal -or $backupRoot -eq $formal -or $candidateRoot -eq $backupRoot) { throw 'Distinct paths required' }
foreach ($name in @('RecruitOps-Desktop-Preview.exe', 'resources/app.asar', 'resources/desktop-runtime/runtime-manifest.json', 'README-PORTABLE.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $candidateRoot $name) -PathType Leaf)) { throw "Incomplete candidate: $name" }
}
if (Test-Path -LiteralPath (Join-Path $candidateRoot '.data')) { throw 'Candidate contains user data' }
$entries = @(Get-ChildItem -LiteralPath $candidateRoot -Force)
$names = @($entries.Name)
foreach ($item in @(Get-ChildItem -LiteralPath $formal -Force)) {
    if ($item.Name -ne '.data' -and $item.Name -notin $names) { throw "Unexpected formal root entry: $($item.Name)" }
    Assert-Child $formal $item.FullName
}
$data = Join-Path $formal '.data'
if (-not (Test-Path -LiteralPath $data -PathType Container)) { throw 'Existing data root missing' }
Assert-Stopped $formal
$beforeData = Get-TreeSummary $data
$expected = Get-TreeSummary $candidateRoot -ContentHashes
$old = Get-TreeSummary $formal -ContentHashes -SkipData
[void]@(Get-ProgramTargets $formal $names)
if ($CopyOnly) {
    Invoke-CopyOnlyPromotion $candidateRoot $formal $backupRoot $beforeData $expected $old -ReuseBackup:$ReuseBackup -Apply:$Apply
    exit 0
}
if (-not $Apply) {
    [PSCustomObject]@{ Apply = $false; Formal = $formal; Candidate = $candidateRoot; Backup = $backupRoot; Data = $beforeData; CandidateFiles = $expected.Files } | ConvertTo-Json -Depth 4
    exit 0
}
Assert-Stopped $formal
New-Item -ItemType Directory -Path $backupRoot | Out-Null
$oldProgram = Join-Path $backupRoot 'program'
New-Item -ItemType Directory -Path $oldProgram | Out-Null
$programRemovalStarted = $false
try {
    # Backup may be on another volume. Copy and verify every program byte before
    # deleting anything from the original installation to release disk space.
    Copy-ProgramTree $formal $oldProgram $data
    if (Test-Path -LiteralPath (Join-Path $oldProgram '.data')) { throw 'Program backup unexpectedly contains user data' }
    if ((Get-TreeSummary $oldProgram -ContentHashes).Digest -ne $old.Digest) { throw 'Old program backup hash mismatch' }
    Assert-Stopped $formal
    if ((Get-TreeSummary $formal -ContentHashes -SkipData).Digest -ne $old.Digest) { throw 'Formal program changed while making its backup' }
    if ((Get-TreeSummary $data).Digest -ne $beforeData.Digest) { throw 'Data inventory changed before promotion' }
    [void]@(Get-ProgramTargets $formal $names)
    Assert-Stopped $formal
    $programRemovalStarted = $true
    Remove-ProgramTargets $formal $names
    Copy-ProgramTree $candidateRoot $formal
    $actual = Get-TreeSummary $formal -ContentHashes -SkipData
    if ($actual.Digest -ne $expected.Digest) { throw 'Formal files differ from candidate' }
    $afterData = Get-TreeSummary $data
    if ($afterData.Digest -ne $beforeData.Digest) { throw 'Data inventory changed during promotion' }
    Assert-Stopped $formal
    $report = [PSCustomObject]@{
        Formal = $formal; Candidate = $candidateRoot; Backup = $oldProgram
        ProgramFiles = $actual.Files; ProgramBytes = $actual.Bytes; ProgramDigest = $actual.Digest
        DataUnchanged = $true; DataFiles = $afterData.Files; DataBytes = $afterData.Bytes
        DataInventoryDigest = $afterData.Digest; DesktopStarted = $false
        CompletedUtc = [DateTime]::UtcNow.ToString('o')
    }
    $report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $backupRoot 'promotion-report.json') -Encoding utf8
    $report | ConvertTo-Json -Depth 4
} catch {
    $failure = $_
    if ($programRemovalStarted) {
        try {
            # Never move/delete files under a newly started desktop. Both the
            # original program backup and candidate remain available untouched.
            Assert-Stopped $formal
            if ((Get-TreeSummary $oldProgram -ContentHashes).Digest -ne $old.Digest) { throw 'Rollback backup hash mismatch' }
            if (Test-Path -LiteralPath (Join-Path $oldProgram '.data')) { throw 'Rollback backup unexpectedly contains user data' }
            [void]@(Get-ProgramTargets $formal $names)
            Assert-Stopped $formal
            Remove-ProgramTargets $formal $names
            Copy-ProgramTree $oldProgram $formal
            if ((Get-TreeSummary $formal -ContentHashes -SkipData).Digest -ne $old.Digest) { throw 'Restored program hash mismatch' }
            Assert-Stopped $formal
        } catch {
            throw "Promotion failed ($($failure.Exception.Message)); automatic rollback stopped ($($_.Exception.Message)). Program backup retained at: $oldProgram. User data was not moved or deleted."
        }
    }
    throw $failure
}
