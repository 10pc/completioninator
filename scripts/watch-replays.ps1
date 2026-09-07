<#
.SYNOPSIS
  Watches the local osu! Replays folder and syncs new replays to the NAS.

.DESCRIPTION
  Event-driven via FileSystemWatcher (Created/Renamed for *.osr): after
  activity settles for DebounceSeconds, runs sync-replays.ps1. A periodic
  full sync (FullSyncMinutes) covers any missed events — watchers can drop
  notifications under burst load, so the timer is the real guarantee and
  the events are just the fast path.

  Run at logon via install-watcher.ps1 (scheduled task), or manually.

.EXAMPLE
  .\watch-replays.ps1 -Source 'C:\Users\user\osu!\Replays' -Destination 'S:\osu\replays'
#>
[CmdletBinding()]
param(
  [string]$Source = $env:OSU_REPLAYS_SOURCE,
  [string]$Destination = $env:NAS_REPLAYS_DEST,
  [int]$DebounceSeconds = 30,
  [int]$FullSyncMinutes = 30,
  [string]$LogPath = (Join-Path ([System.IO.Path]::GetTempPath()) "osu-watch-replays.log")
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Source)) { throw "Source not set. Pass -Source or set `$env:OSU_REPLAYS_SOURCE." }
if ([string]::IsNullOrWhiteSpace($Destination)) { throw "Destination not set. Pass -Destination or set `$env:NAS_REPLAYS_DEST." }
if (-not (Test-Path -LiteralPath $Source)) { throw "Source not found: $Source" }

$syncScript = Join-Path $PSScriptRoot "sync-replays.ps1"
if (-not (Test-Path -LiteralPath $syncScript)) { throw "sync script not found: $syncScript" }

# Single instance: the hourly watchdog trigger must not stack a second copy.
# Held for the process lifetime; an abandoned mutex means the previous owner
# died, which is exactly when a new instance SHOULD proceed.
$mutex = New-Object System.Threading.Mutex($false, "Global\OsuReplayWatcher")
$acquired = $false
try {
  $acquired = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
  $acquired = $true
}
if (-not $acquired) {
  Write-Output "another watcher instance is already running; exiting"
  exit 0
}

function Write-WatchLog([string]$Message) {
  $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
  Write-Output $line
  Add-Content -LiteralPath $LogPath -Value $line -ErrorAction SilentlyContinue
}

function Invoke-ReplaySync([string]$Reason) {
  Write-WatchLog "sync start ($Reason)"
  try {
    $out = & $syncScript -Source $Source -Destination $Destination 2>&1 | Out-String
    Write-WatchLog "sync done: $($out.Trim())"
  } catch {
    Write-WatchLog "sync FAILED: $($_.Exception.Message)"
  }
}

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = (Resolve-Path -LiteralPath $Source).Path
$watcher.Filter = "*.osr"
$watcher.IncludeSubdirectories = $true
$watcher.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor
                       [System.IO.NotifyFilters]::LastWrite -bor
                       [System.IO.NotifyFilters]::Size
$watcher.EnableRaisingEvents = $true

$state = @{ LastEvent = [DateTime]::MinValue; LastFullSync = [DateTime]::MinValue }
$onEvent = {
  $Event.MessageData.LastEvent = Get-Date
}
$subs = @(
  Register-ObjectEvent -InputObject $watcher -EventName Created -Action $onEvent -MessageData $state
  Register-ObjectEvent -InputObject $watcher -EventName Renamed -Action $onEvent -MessageData $state
)

Write-WatchLog "watching $Source -> $Destination (debounce ${DebounceSeconds}s, full sync every ${FullSyncMinutes}m)"
Invoke-ReplaySync "startup"
$state.LastFullSync = Get-Date

try {
  while ($true) {
    Start-Sleep -Seconds 5
    $now = Get-Date
    $idleFor = ($now - $state.LastEvent).TotalSeconds
    $sinceFull = ($now - $state.LastFullSync).TotalMinutes
    if ($sinceFull -ge $FullSyncMinutes) {
      $state.LastFullSync = $now
      Invoke-ReplaySync "periodic"
    } elseif ($state.LastEvent -gt [DateTime]::MinValue -and $idleFor -ge $DebounceSeconds) {
      $state.LastEvent = [DateTime]::MinValue  # consume; periodic remains the backstop
      Invoke-ReplaySync "settled"
    }
  }
} finally {
  $subs | ForEach-Object { Unregister-Event -SourceIdentifier $_.Name -ErrorAction SilentlyContinue }
  $watcher.Dispose()
}
