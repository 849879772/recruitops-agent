[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$PythonExecutable = "",
    [string]$TaskNamePrefix = "RecruitOps",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Join-Path $PSScriptRoot ".."
}

function ConvertTo-WindowsCommandLineArgument {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Value
    )

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') {
        return $Value
    }

    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            for ($index = 0; $index -lt (($backslashes * 2) + 1); $index += 1) {
                [void]$builder.Append('\')
            }
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        for ($index = 0; $index -lt $backslashes; $index += 1) {
            [void]$builder.Append('\')
        }
        $backslashes = 0
        [void]$builder.Append($character)
    }
    for ($index = 0; $index -lt ($backslashes * 2); $index += 1) {
        [void]$builder.Append('\')
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Format-ArgumentList {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    return (($Arguments | ForEach-Object {
        ConvertTo-WindowsCommandLineArgument -Value $_
    }) -join ' ')
}

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path
$runner = Join-Path $resolvedRoot "scripts\run_local_task.py"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "local task runner was not found: $runner"
}
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $venvPython = Join-Path $resolvedRoot ".venv\Scripts\python.exe"
    $PythonExecutable = if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $venvPython
    } else {
        "python"
    }
}
if ([System.IO.Path]::IsPathRooted($PythonExecutable) -and
    -not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Python executable was not found: $PythonExecutable"
}
if ($TaskNamePrefix -notmatch '^[A-Za-z0-9._-]+$') {
    throw "TaskNamePrefix may contain only letters, digits, dot, underscore, and hyphen"
}

$tasks = @(
    [pscustomobject]@{ Id = "daily_recruitment_intelligence"; StartTime = "08:00" }
    [pscustomobject]@{ Id = "crawler_health"; StartTime = "08:15" }
    [pscustomobject]@{ Id = "application_progress"; StartTime = "20:00" }
    [pscustomobject]@{ Id = "recruitment_mailbox"; StartTime = "09:00" }
)

foreach ($task in $tasks) {
    $taskName = "$TaskNamePrefix-$($task.Id)"
    $taskRun = @(
        (ConvertTo-WindowsCommandLineArgument -Value $PythonExecutable)
        (ConvertTo-WindowsCommandLineArgument -Value $runner)
        "--task"
        (ConvertTo-WindowsCommandLineArgument -Value $task.Id)
    ) -join ' '
    $schtasksArguments = @(
        "/Create"
        "/TN"
        $taskName
        "/SC"
        "DAILY"
        "/ST"
        $task.StartTime
        "/TR"
        $taskRun
        "/F"
    )

    if ($DryRun) {
        Write-Output ("DRY-RUN schtasks.exe " + (Format-ArgumentList -Arguments $schtasksArguments))
        continue
    }

    & schtasks.exe @schtasksArguments
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks failed while creating $taskName with exit code $LASTEXITCODE"
    }
}
