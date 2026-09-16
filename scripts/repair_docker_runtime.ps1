[CmdletBinding()]
param([switch]$Apply)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (Get-Process -Name 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue) {
    throw 'Quit Docker Desktop first. This script will not terminate running containers or processes.'
}
$docker = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
if (-not (Test-Path -LiteralPath $docker -PathType Leaf)) { throw 'Docker Desktop executable not found.' }
$targets = @(
    [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Docker\run')),
    [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'docker-secrets-engine'))
)
$allowed = @('dockerInference', 'userAnalyticsOtlpHttp.sock', 'engine.sock')
$plans = @()
foreach ($path in $targets) {
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $item = Get-Item -LiteralPath $path -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Unexpected runtime directory: $path"
    }
    foreach ($child in @(Get-ChildItem -LiteralPath $path -Force)) {
        if ($child.PSIsContainer -or $child.Length -ne 0 -or $child.Name -notin $allowed) {
            throw "Unexpected content; refusing to move directory: $path"
        }
    }
    $destination = $path + '.stale-' + [guid]::NewGuid().ToString('N')
    if ([IO.Path]::GetDirectoryName($destination) -ne [IO.Path]::GetDirectoryName($path)) {
        throw 'Destination is outside the runtime parent.'
    }
    $plans += @{ Source = $path; Destination = $destination }
}
foreach ($plan in $plans) {
    if ($Apply) {
        Rename-Item -LiteralPath $plan.Source -NewName ([IO.Path]::GetFileName($plan.Destination))
    }
    Write-Output "Preserve runtime directory: $($plan.Source) -> $($plan.Destination)"
}
if ($Apply) {
    Start-Process -FilePath $docker -WindowStyle Hidden
    Write-Output 'Docker Desktop launched. Verify docker info and container health separately.'
} else {
    Write-Output 'Preview only. Use -Apply after quitting Docker Desktop. No files changed.'
}
