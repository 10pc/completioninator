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
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument @(
  "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$watcher`"",
  "-Source", "`"$Source`"", "-Destination", "`"$Destination`""
)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Description "Sync new osu! replays to NAS" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "installed and started task $TaskName"
