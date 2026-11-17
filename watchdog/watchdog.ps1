<#
Safety net for the forecasting bot. It is meant to run once an hour on the owner's own computer.

The bot keeps time inside its GitHub repository (see timer.py). If both of its timers were lost
at the same moment, for example in a long GitHub outage, nothing on GitHub would restart them.
This script notices that no forecasting run has started for a while and starts one. Every
forecasting run restarts the timers as its last step.

It needs the GitHub CLI (gh), signed in to an account that may run the repository's workflows.
install.ps1 registers it as a scheduled task. Keep this file plain ASCII: Windows PowerShell
reads script files without a byte order mark as ANSI.
#>
[CmdletBinding()]
param(
    [string]$Repo = "MarcSipflip/northsignal-bot",
    # The timers start a forecasting run about every 15 minutes: this is two missed runs plus a margin.
    [int]$QuietMinutes = 35,
    # Same limit as FORECAST_STUCK_SECONDS in timer.py: an unfinished run older than this is cancelled.
    [int]$StuckMinutes = 90,
    # Empty means watchdog.log next to this file.
    [string]$LogPath = "",
    [int]$LogLinesKept = 500
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Not a default value above: Windows PowerShell leaves $PSScriptRoot empty there when started with -File.
if (-not $LogPath) { $LogPath = Join-Path $PSScriptRoot "watchdog.log" }

$ForecastWorkflow = "run_bot_on_tournament.yaml"
$TimerWorkflow = "timer.yaml"

function ConvertTo-UtcTime {
    # GitHub sends times like 2026-11-17T09:35:24Z. PowerShell 7 has already turned them into dates.
    param([object]$Value)
    if ($Value -is [datetime]) { return $Value.ToUniversalTime() }
    $styles = [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal
    return [datetime]::Parse([string]$Value, [cultureinfo]::InvariantCulture, $styles)
}

function Get-WatchdogDecision {
    # Decides what to do from GitHub's answers. It sends nothing itself, so tests.ps1 can check it.
    param(
        [object[]]$Workflows,  # the repository's workflows: path and state
        [object[]]$Runs,       # recent forecasting runs: databaseId, status and createdAt
        [datetime]$Now,        # in UTC
        [int]$QuietMinutes,
        [int]$StuckMinutes
    )
    $decision = [pscustomobject]@{ Start = $false; Enable = $false; Cancel = @(); Reason = "" }

    $states = @{}
    foreach ($workflow in $Workflows) { $states[(Split-Path $workflow.path -Leaf)] = $workflow.state }
    if (-not $states.ContainsKey($ForecastWorkflow)) { throw "The repository has no workflow file named $ForecastWorkflow." }
    foreach ($name in $ForecastWorkflow, $TimerWorkflow) {
        if ($states[$name] -eq "disabled_manually") {
            $decision.Reason = "paused: $name was switched off by hand, so nothing is started"
            return $decision
        }
    }

    $newest = $null    # minutes since the newest run was created
    $underWay = $null  # the same for the newest run that has not finished and is not stuck
    $stuck = @()
    foreach ($run in $Runs) {
        $age = ($Now - (ConvertTo-UtcTime $run.createdAt)).TotalMinutes
        if ($null -eq $newest -or $age -lt $newest) { $newest = $age }
        if ($run.status -ne "completed") {
            if ($age -gt $StuckMinutes) { $stuck += $run.databaseId }
            elseif ($null -eq $underWay -or $age -lt $underWay) { $underWay = $age }
        }
    }

    if ($null -ne $underWay) {
        $decision.Reason = "healthy: a forecasting run is under way, started {0} min ago" -f [int][math]::Floor($underWay)
        return $decision
    }
    if ($null -ne $newest -and $newest -lt $QuietMinutes) {
        $decision.Reason = "healthy: the last forecasting run started {0} min ago" -f [int][math]::Floor($newest)
        return $decision
    }

    $decision.Start = $true
    $decision.Enable = ($states[$ForecastWorkflow] -eq "disabled_inactivity")
    $decision.Cancel = $stuck
    if ($null -eq $newest) { $decision.Reason = "no forecasting run was found" }
    else { $decision.Reason = "no forecasting run has started for {0} min" -f [int][math]::Floor($newest) }
    return $decision
}

function Invoke-Gh {
    # Runs gh and returns what it printed. Throws with gh's own message when it fails or hangs.
    param([string]$GhPath, [string[]]$Arguments, [int]$TimeoutSeconds = 60)
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $GhPath
    $info.Arguments = ($Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join " "
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $process = [System.Diagnostics.Process]::Start($info)
    try {
        $output = $process.StandardOutput.ReadToEndAsync()
        $problem = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            $process.Kill()
            throw "gh $($Arguments -join ' ') did not answer within $TimeoutSeconds seconds"
        }
        $process.WaitForExit()  # lets the rest of the output arrive
        if ($process.ExitCode -ne 0) {
            throw "gh $($Arguments -join ' ') failed: $(($problem.Result -replace '\s+', ' ').Trim())"
        }
        return $output.Result
    } finally {
        $process.Dispose()
    }
}

function Write-WatchdogLog {
    # One line per check, in local time. Only the newest lines are kept.
    param([string]$Message)
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Output $line
    $folder = Split-Path -Parent $LogPath
    if ($folder -and -not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Force -Path $folder | Out-Null }
    $kept = @()
    if (Test-Path -LiteralPath $LogPath) { $kept = @(Get-Content -LiteralPath $LogPath -Tail ($LogLinesKept - 1)) }
    Set-Content -LiteralPath $LogPath -Value ($kept + $line) -Encoding UTF8
}

if ($MyInvocation.InvocationName -eq ".") { return }  # loaded by tests.ps1, which only wants the functions

try {
    $gh = Get-Command gh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $gh) { throw "the GitHub CLI (gh) was not found on this computer" }

    $workflows = (Invoke-Gh $gh.Source @("api", "repos/$Repo/actions/workflows?per_page=100") | ConvertFrom-Json).workflows
    $runs = Invoke-Gh $gh.Source @("run", "list", "-R", $Repo, "--workflow", $ForecastWorkflow, "--limit", "20", "--json", "databaseId,status,createdAt") | ConvertFrom-Json
    $runs = @($runs | ForEach-Object { $_ })  # Windows PowerShell hands the list over as one object

    $decision = Get-WatchdogDecision -Workflows $workflows -Runs $runs -Now ([datetime]::UtcNow) -QuietMinutes $QuietMinutes -StuckMinutes $StuckMinutes
    if (-not $decision.Start) {
        Write-WatchdogLog $decision.Reason
        exit 0
    }

    if ($decision.Enable) {
        # GitHub switches scheduled workflows off after 60 days without repository activity.
        Invoke-Gh $gh.Source @("workflow", "enable", $ForecastWorkflow, "-R", $Repo) | Out-Null
    }
    foreach ($id in $decision.Cancel) {
        # A run that never finished would keep the new one waiting in line behind it.
        try { Invoke-Gh $gh.Source @("run", "cancel", "$id", "-R", $Repo) | Out-Null }
        catch { Write-WatchdogLog "could not cancel the stuck run ${id}: $($_.Exception.Message)" }
    }
    Invoke-Gh $gh.Source @("workflow", "run", $ForecastWorkflow, "-R", $Repo) | Out-Null
    Write-WatchdogLog "repaired: $($decision.Reason), so one was started"
    exit 0
} catch {
    Write-WatchdogLog "error: $($_.Exception.Message)"
    exit 1
}
