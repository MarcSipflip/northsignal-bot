# Checks the watchdog's decisions against made-up GitHub answers. Nothing is sent to GitHub.
#
#     powershell -ExecutionPolicy Bypass -File watchdog\tests.ps1    (Windows PowerShell 5.1)
#     pwsh -File watchdog/tests.ps1                                  (PowerShell 7, also on Linux)

. (Join-Path $PSScriptRoot "watchdog.ps1")

$now = [datetime]::new(2026, 11, 17, 12, 0, 0, [System.DateTimeKind]::Utc)
$failures = 0

function New-Run {
    # As Windows PowerShell reads GitHub's answer: the time is still text.
    param([int]$Id, [double]$MinutesAgo, [string]$Status = "completed")
    [pscustomobject]@{ databaseId = $Id; status = $Status; createdAt = $now.AddMinutes(-$MinutesAgo).ToString("yyyy-MM-dd'T'HH:mm:ss'Z'") }
}

function New-Workflows {
    param([string]$Forecast = "active", [string]$Timer = "active")
    @(
        [pscustomobject]@{ path = ".github/workflows/run_bot_on_tournament.yaml"; state = $Forecast },
        [pscustomobject]@{ path = ".github/workflows/timer.yaml"; state = $Timer },
        [pscustomobject]@{ path = ".github/workflows/tests.yaml"; state = "active" }
    )
}

function Get-Decision {
    param([object[]]$Runs, [object[]]$Workflows = (New-Workflows))
    Get-WatchdogDecision -Workflows $Workflows -Runs $Runs -Now $now -QuietMinutes 35 -StuckMinutes 90
}

function Assert-Decision {
    param([string]$Case, [object]$Decision, [bool]$Start, [string]$ReasonLike, [bool]$Enable = $false, [string]$Cancel = "")
    $problems = @()
    if ($Decision.Start -ne $Start) { $problems += "Start is $($Decision.Start), expected $Start" }
    if ($Decision.Enable -ne $Enable) { $problems += "Enable is $($Decision.Enable), expected $Enable" }
    if (($Decision.Cancel -join ",") -ne $Cancel) { $problems += "Cancel is '$($Decision.Cancel -join ',')', expected '$Cancel'" }
    if ($Decision.Reason -notlike $ReasonLike) { $problems += "Reason is '$($Decision.Reason)', expected like '$ReasonLike'" }
    if ($problems) {
        $script:failures++
        Write-Output "FAIL  $Case"
        $problems | ForEach-Object { Write-Output "      $_" }
    } else {
        Write-Output "ok    $Case"
    }
}

Assert-Decision "a run started 10 minutes ago: nothing to do" `
    (Get-Decision @((New-Run 3 10), (New-Run 2 25), (New-Run 1 40))) $false "healthy: the last forecasting run started 10 min ago"

Assert-Decision "34 minutes of quiet is still inside the limit" `
    (Get-Decision @((New-Run 1 34))) $false "healthy:*"

Assert-Decision "36 minutes of quiet starts a run" `
    (Get-Decision @((New-Run 2 36), (New-Run 1 51))) $true "no forecasting run has started for 36 min"

Assert-Decision "the newest run counts even when GitHub lists it last" `
    (Get-Decision @((New-Run 1 300), (New-Run 2 200), (New-Run 3 5))) $false "healthy: the last forecasting run started 5 min ago"

Assert-Decision "a long run that is still going is left alone" `
    (Get-Decision @((New-Run 2 60 "in_progress"), (New-Run 1 75))) $false "healthy: a forecasting run is under way, started 60 min ago"

Assert-Decision "a run waiting in line counts as under way" `
    (Get-Decision @((New-Run 2 50 "queued"), (New-Run 1 75))) $false "healthy: a forecasting run is under way*"

Assert-Decision "a run that never finished is cancelled and replaced" `
    (Get-Decision @((New-Run 7 120 "in_progress"), (New-Run 6 135))) $true "no forecasting run has started for 120 min" -Cancel "7"

Assert-Decision "a stuck run is left to the timers while fresh runs still arrive" `
    (Get-Decision @((New-Run 8 10), (New-Run 7 120 "in_progress"))) $false "healthy: the last forecasting run started 10 min ago"

Assert-Decision "no run at all starts one" `
    (Get-Decision @()) $true "no forecasting run was found"

Assert-Decision "forecasting switched off by hand: the bot is paused" `
    (Get-Decision @((New-Run 1 600)) (New-Workflows -Forecast "disabled_manually")) $false "paused: run_bot_on_tournament.yaml*"

Assert-Decision "timers switched off by hand: the bot is paused" `
    (Get-Decision @((New-Run 1 600)) (New-Workflows -Timer "disabled_manually")) $false "paused: timer.yaml*"

Assert-Decision "forecasting switched off by GitHub for inactivity is switched back on" `
    (Get-Decision @((New-Run 1 600)) (New-Workflows -Forecast "disabled_inactivity")) $true "no forecasting run has started for 600 min" -Enable $true

Assert-Decision "inactivity does not matter while runs still arrive" `
    (Get-Decision @((New-Run 1 5)) (New-Workflows -Forecast "disabled_inactivity")) $false "healthy:*"

# PowerShell 7 turns GitHub's times into dates before the watchdog sees them.
$asDates = @(
    [pscustomobject]@{ databaseId = 2; status = "completed"; createdAt = $now.AddMinutes(-36) },
    [pscustomobject]@{ databaseId = 1; status = "completed"; createdAt = $now.AddMinutes(-51).ToLocalTime() }
)
Assert-Decision "times that are already dates give the same answer" (Get-Decision $asDates) $true "no forecasting run has started for 36 min"

$refused = $false
try { Get-Decision @((New-Run 1 5)) @([pscustomobject]@{ path = ".github/workflows/tests.yaml"; state = "active" }) | Out-Null }
catch { $refused = $true }
if ($refused) { Write-Output "ok    a repository without the forecasting workflow is reported as an error" }
else { $failures++; Write-Output "FAIL  a repository without the forecasting workflow is reported as an error" }

if ($failures) { Write-Output "$failures check(s) failed."; exit 1 }
Write-Output "All checks passed."
