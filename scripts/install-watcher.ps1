<#
.SYNOPSIS
  Installs (or removes) the logon scheduled task for watch-replays.ps1.

.EXAMPLE
  .\install-watcher.ps1 -Source 'C:\Users\user\osu!\Replays' -Destination 'S:\osu\replays'
  .\install-watcher.ps1 -Uninstall
#>
[CmdletBinding()]
param(
  [string]$Source = $env:OSU_REPLAYS_SOURCE,
  [string]$Destination = $env:NAS_REPLAYS_DEST,
  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "OsuReplayWatcher"

if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Output "removed task $TaskName (if it existed)"
  return
}

if ([string]::IsNullOrWhiteSpace($Source)) { throw "Source not set. Pass -Source or set `$env:OSU_REPLAYS_SOURCE." }
if ([string]::IsNullOrWhiteSpace($Destination)) { throw "Destination not set. Pass -Destination or set `$env:NAS_REPLAYS_DEST." }

$watcher = Join-Path $PSScriptRoot "watch-replays.ps1"
# conhost --headless: -WindowStyle Hidden is ignored when Windows Terminal is
# the default console host (it still opens a visible tab). Hosting the shell
# under a headless conhost never shows a window, hourly trigger included.
$argList = "-NoProfile -ExecutionPolicy Bypass -File `"$watcher`" " +
           "-Source `"$Source`" -Destination `"$Destination`""
$action = New-ScheduledTaskAction -Execute "conhost.exe" `
  -Argument "--headless powershell.exe $argList"
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Watchdog: hourly forever would exceed the scheduler's duration range, so
# repeat for 30 days at a time (logon trigger covers reboots regardless).
$triggerHourly = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
  -RepetitionInterval (New-TimeSpan -Hours 1) `
  -RepetitionDuration (New-TimeSpan -Days 30)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($triggerLogon, $triggerHourly) `
  -Settings $settings -Description "Sync new osu! replays to NAS" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "installed and started task $TaskName"
