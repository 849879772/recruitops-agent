[CmdletBinding()]
param(
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$pidPath = Join-Path $projectRoot ".data\recruitops-api.pid.json"
if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    Write-Output "RecruitOps API is not recorded as running."
    exit 0
}
$record = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
$pidValue = [int]$record.pid
$expectedExecutable = [System.IO.Path]::GetFullPath([string]$record.executable)
$process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
if ($null -eq $process) {
    if (-not $DryRun) {
        Remove-Item -LiteralPath $pidPath -Force
    }
    Write-Output "RecruitOps API process is no longer running."
    exit 0
}
$actualExecutable = [System.IO.Path]::GetFullPath([string]$process.Path)
if (-not $actualExecutable.Equals($expectedExecutable, [StringComparison]::OrdinalIgnoreCase)) {
    throw "PID $pidValue no longer belongs to the recorded RecruitOps executable"
}
if ($DryRun) {
    Write-Output "DRY-RUN stop RecruitOps API PID $pidValue"
    exit 0
}

Stop-Process -Id $pidValue -ErrorAction Stop
Remove-Item -LiteralPath $pidPath -Force
Write-Output "RecruitOps API stopped (PID $pidValue)."
