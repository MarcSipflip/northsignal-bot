<#
Registers the watchdog as a scheduled task for the current Windows user, or removes it.

    powershell -ExecutionPolicy Bypass -File watchdog\install.ps1            # install or update
    powershell -ExecutionPolicy Bypass -File watchdog\install.ps1 -Remove    # remove

The two script files are copied to %LOCALAPPDATA%\northsignal-bot\watchdog and the task runs
that copy once an hour, so it keeps working whatever happens to this folder. The task runs only
while this user is signed in to Windows, because it relies on this user's GitHub CLI sign-in.
No administrator rights are needed. Keep this file plain ASCII.
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    # The first scheduled check; later ones follow every hour from that time.
    [int]$FirstRunInMinutes = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "NorthSignal bot watchdog"
$Target = Join-Path $env:LOCALAPPDATA "northsignal-bot\watchdog"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
    Write-Output "Removed the task '$TaskName' and the folder $Target."
    return
}

New-Item -ItemType Directory -Force -Path $Target | Out-Null
foreach ($file in "watchdog.ps1", "run-hidden.vbs") {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $Target $file) -Force
}

# One check right away with the installed copy. It shows whether gh is present and signed in.
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$firstCheck = & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $Target "watchdog.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "The first check failed, so the task was not registered. It said: $firstCheck"
}

$launcher = Join-Path $Target "run-hidden.vbs"
$action = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot "System32\wscript.exe") -Argument "//B //Nologo `"$launcher`""
$start = (Get-Date).AddMinutes($FirstRunInMinutes)
$start = $start.AddSeconds(-$start.Second)
# No -RepetitionDuration: on Windows 10 and later that means the task repeats without an end.
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$description = "Once an hour, checks that the NorthSignal forecasting bot on GitHub is still starting runs, and starts one " +
    "if it has gone quiet. Installed by watchdog\install.ps1 from the northsignal-bot repository. " +
    "Log: $Target\watchdog.log"
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description $description -Force | Out-Null

Write-Output "First check: $firstCheck"
Write-Output "Registered the task '$TaskName' for $user."
Write-Output "First scheduled check at $($start.ToString('HH:mm')), then every hour. Log: $Target\watchdog.log"
