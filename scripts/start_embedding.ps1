[CmdletBinding()]
param([switch]$Force, [int]$WaitSeconds = 180)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$configPath = Join-Path $root '.data\embedding\service.json'
if (-not (Test-Path -LiteralPath $configPath)) {
    if ($Force) { throw 'Run configure_qwen_embedding.py first.' }
    return
}
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if (-not $config.enabled -and -not $Force) { return }
$headers = @{ Authorization = ('Bearer ' + $config.api_key) }
$url = "http://127.0.0.1:$($config.port)/health"
$mutex = [Threading.Mutex]::new($false, 'Local\RecruitOpsEmbeddingLaunch')
$locked = $false
function Test-EmbeddingReady {
    try {
        $health = Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 3 -NoProxy
        return $health.ready -and $health.model -eq 'Qwen/Qwen3-Embedding-0.6B' -and $health.device -eq 'cuda'
    } catch { return $false }
}
try {
    try { $locked = $mutex.WaitOne(10000) }
    catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another embedding startup is in progress.' }
    if (Test-EmbeddingReady) { Write-Output 'Qwen GPU embedding service is ready.'; return }
    $python = Join-Path $root '.data\embedding\venv\Scripts\python.exe'
    $server = Join-Path $root 'services\embedding\server.py'
    $pidPath = Join-Path $root '.data\embedding\process.json'
    $existing = $null
    if (Test-Path -LiteralPath $pidPath) {
        $saved = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
        $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$($saved.pid)"
        if ($candidate -and $candidate.CommandLine -and $candidate.CommandLine.Contains($server)) { $existing = $candidate }
    }
    if (-not $existing) {
        if (Get-NetTCPConnection -State Listen -LocalPort $config.port -ErrorAction SilentlyContinue) {
            throw 'Embedding port is occupied by an unverified service; nothing was stopped.'
        }
        if (-not (Test-Path -LiteralPath $python)) { throw 'Embedding virtual environment missing.' }
        $logRoot = Join-Path $root '.data\logs'
        New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
        $process = Start-Process -FilePath $python -ArgumentList @(('"' + $server + '"'), '--config', ('"' + $configPath + '"')) `
            -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logRoot 'embedding.stdout.log') `
            -RedirectStandardError (Join-Path $logRoot 'embedding.stderr.log')
        @{pid=$process.Id; started_at=[DateTimeOffset]::Now.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding utf8
    }
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($WaitSeconds)
    while (-not (Test-EmbeddingReady)) {
        if ([DateTimeOffset]::UtcNow -ge $deadline) { throw 'Qwen did not become ready. See .data/logs/embedding.stderr.log.' }
        Start-Sleep -Seconds 2
    }
    Write-Output 'Qwen GPU embedding service is ready.'
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
