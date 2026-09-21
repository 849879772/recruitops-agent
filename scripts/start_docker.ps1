[CmdletBinding()]
param(
    [int]$WaitSeconds = 120,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ($WaitSeconds -lt 10 -or $WaitSeconds -gt 600) {
    throw 'WaitSeconds must be between 10 and 600.'
}
if ($DryRun) {
    Write-Output "DRY-RUN: check Docker, recover stopped Desktop runtime if needed, then start existing services in $projectRoot."
    Write-Output 'No image build, database reset, volume removal, or scheduled task creation.'
    exit 0
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI was not found. No files or services were changed.'
}

function Test-DockerReady {
    & docker info --format '{{.ServerVersion}}' *> $null
    return $LASTEXITCODE -eq 0
}

if (-not (Test-DockerReady)) {
    $desktop = @(Get-Process -Name 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue)
    if ($desktop.Count -eq 0) {
        # The repair helper only preserves verified, empty socket directories.
        & (Join-Path $PSScriptRoot 'repair_docker_runtime.ps1') -Apply
    } else {
        Write-Output 'Docker Desktop is running. Waiting without terminating it or moving its runtime files.'
    }
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($WaitSeconds)
    while (-not (Test-DockerReady)) {
        if ([DateTimeOffset]::UtcNow -ge $deadline) {
            throw 'Docker did not become ready. Inspect the Desktop error; no containers or volumes were removed.'
        }
        Start-Sleep -Seconds 2
    }
}

Push-Location -LiteralPath $projectRoot
try {
    & docker compose up -d --no-build --wait --wait-timeout $WaitSeconds postgres api
    if ($LASTEXITCODE -ne 0) {
        throw 'RecruitOps services did not pass startup checks. Existing state was preserved.'
    }
    & docker compose ps
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect service status.' }
} finally {
    Pop-Location
}
