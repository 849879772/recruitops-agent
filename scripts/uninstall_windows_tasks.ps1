[CmdletBinding()]
param(
    [string]$TaskNamePrefix = "RecruitOps",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

if ($TaskNamePrefix -notmatch '^[A-Za-z0-9._-]+$') {
    throw "TaskNamePrefix may contain only letters, digits, dot, underscore, and hyphen"
}

$taskIds = @(
    "daily_recruitment_intelligence"
    "crawler_health"
    "application_progress"
    "recruitment_mailbox"
)

foreach ($taskId in $taskIds) {
    $taskName = "$TaskNamePrefix-$taskId"
    $schtasksArguments = @(
        "/Delete"
        "/TN"
        $taskName
        "/F"
    )

    if ($DryRun) {
        Write-Output ("DRY-RUN schtasks.exe " + (Format-ArgumentList -Arguments $schtasksArguments))
        continue
    }

    & schtasks.exe @schtasksArguments
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks failed while deleting $taskName with exit code $LASTEXITCODE"
    }
}
