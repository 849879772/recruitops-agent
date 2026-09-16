[CmdletBinding()]
param([switch]$NoBrowser)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$url = 'http://127.0.0.1:8012/'
$mutex = [Threading.Mutex]::new($false, 'Local\RecruitOpsDesktopLaunch')
$locked = $false
$exitCode = 0

function Test-WorkbenchReady {
    try {
        return (Invoke-WebRequest -Uri ($url + 'ready') -TimeoutSec 5 -NoProxy).StatusCode -eq 200
    } catch {
        return $false
    }
}

try {
    try { $locked = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { exit 0 }
    $logRoot = Join-Path $root '.data\logs'
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $logPath = Join-Path $logRoot 'desktop-start.log'
    & (Join-Path $PSScriptRoot 'start_embedding.ps1')
    if (-not (Test-WorkbenchReady)) {
        & (Join-Path $PSScriptRoot 'start_docker.ps1') -WaitSeconds 180 *>&1 |
            Out-File -LiteralPath $logPath -Encoding utf8
    }
    if (-not (Test-WorkbenchReady)) {
        throw "Workbench is not ready at $url. Check $logPath"
    }
    if (-not $NoBrowser) { Start-Process -FilePath $url }
    Write-Output "RecruitOps ready: $url"
} catch {
    $exitCode = 1
    $message = "RecruitOps could not start.`n$($_.Exception.Message)`nLogs: $root\.data\logs\desktop-start.log"
    if ($NoBrowser) { Write-Output $message }
    else {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show($message, 'RecruitOps', 'OK', 'Error') | Out-Null
    }
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
exit $exitCode
