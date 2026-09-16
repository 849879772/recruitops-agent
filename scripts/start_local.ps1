[CmdletBinding()]
param(
    [int]$Port = 8010,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "project virtual environment was not found: $python"
}
if ($Port -lt 1 -or $Port -gt 65535) {
    throw "Port must be between 1 and 65535"
}
$dataRoot = Join-Path $projectRoot ".data"
$logRoot = Join-Path $dataRoot "logs"
$pidPath = Join-Path $dataRoot "recruitops-api.pid.json"
$arguments = @(
    "-m"
    "uvicorn"
    "apps.api.main:app"
    "--host"
    "127.0.0.1"
    "--port"
    [string]$Port
)

if ($DryRun) {
    Write-Output "DRY-RUN start RecruitOps API on 127.0.0.1:$Port"
    exit 0
}

if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $existing = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
    $process = Get-Process -Id ([int]$existing.pid) -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        throw "RecruitOps API is already running with PID $($existing.pid)"
    }
    Remove-Item -LiteralPath $pidPath -Force
}

New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$stdout = Join-Path $logRoot "api.stdout.log"
$stderr = Join-Path $logRoot "api.stderr.log"
$process = Start-Process `
    -FilePath $python `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

[pscustomobject]@{
    pid = $process.Id
    executable = $python
    port = $Port
    started_at = [DateTimeOffset]::Now.ToString("o")
} | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8

Write-Output "RecruitOps API started: http://127.0.0.1:$Port/ (PID $($process.Id))"
