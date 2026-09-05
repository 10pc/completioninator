<#
.SYNOPSIS
  Mirror the local osu! Replays folder to the NAS replay archive over SMB.

.DESCRIPTION
  Copies only *.osr files missing at the destination (never overwrites,
  never deletes). Files land in a .staging subdir first, then move into
  place atomically, so the pipeline scanner never sees a partial .osr.

  Defaults come from $env:OSU_REPLAYS_SOURCE / $env:NAS_REPLAYS_DEST.

.EXAMPLE
  .\sync-replays.ps1 -WhatIf
  .\sync-replays.ps1 -Source 'C:\Users\user\osu!\Replays' -Destination '\\nas\osu\replays'
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string]$Source = $env:OSU_REPLAYS_SOURCE,
  [string]$Destination = $env:NAS_REPLAYS_DEST,
  [string]$StagingDirName = ".staging"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Source)) { throw "Source not set. Pass -Source or set `$env:OSU_REPLAYS_SOURCE." }
if ([string]::IsNullOrWhiteSpace($Destination)) { throw "Destination not set. Pass -Destination or set `$env:NAS_REPLAYS_DEST." }
if (-not (Test-Path -LiteralPath $Source)) { throw "Source not found: $Source" }
if (-not (Test-Path -LiteralPath $Destination)) { throw "Destination unreachable: $Destination. Check the NAS/SMB connection first." }

$staging = Join-Path $Destination $StagingDirName

# 0. Plan: which *.osr files are actually missing at the destination?
$planned = @(Get-ChildItem -LiteralPath $Source -Recurse -File -Filter "*.osr" -ErrorAction SilentlyContinue |
  Where-Object {
    $rel = [System.IO.Path]::GetRelativePath($Source, $_.FullName)
    -not (Test-Path -LiteralPath (Join-Path $Destination $rel))
  })
Write-Output "new replays to publish: $($planned.Count)"
$planned | Select-Object -First 10 -ExpandProperty FullName | ForEach-Object { Write-Output "  + $_" }
if ($planned.Count -gt 10) { Write-Output "  ... and $($planned.Count - 10) more" }

if (-not $PSCmdlet.ShouldProcess("$($planned.Count) new replay(s)", "sync to $Destination")) { return }

New-Item -ItemType Directory -Force -Path $staging | Out-Null

# Pre-flight: prove the destination is writable before robocopy runs.
$probe = Join-Path $staging ".write-test"
try {
  [System.IO.File]::WriteAllText($probe, "ok")
  Remove-Item -LiteralPath $probe -Force
} catch {
  throw "Destination not writable ($staging): $($_.Exception.Message). Check SMB permissions."
}

# 1. Bulk copy *.osr (only) into staging. /XC /XN /XO = skip anything already
#    staged (mirror of rsync --ignore-existing); no /MIR so nothing is deleted.
$rcLog = Join-Path ([System.IO.Path]::GetTempPath()) "osu-sync-robocopy.log"
$rcArgs = @($Source, $staging, "*.osr", "/E", "/XC", "/XN", "/XO", "/R:2", "/W:3", "/NJH", "/NJS", "/LOG:$rcLog")
& robocopy @rcArgs | Out-Null
$rc = $LASTEXITCODE
if ($rc -ge 8) {
  $tail = if (Test-Path -LiteralPath $rcLog) { (Get-Content -LiteralPath $rcLog -Tail 15) -join "`n" } else { "(no log)" }
  throw "robocopy failed with exit code $rc. Log tail:`n$tail"
}

# 2. Move staged files into place (same-share move = atomic rename).
$copied = 0; $skipped = 0
$files = Get-ChildItem -LiteralPath $staging -Recurse -File -Filter "*.osr" -ErrorAction SilentlyContinue
foreach ($f in $files) {
  $rel = [System.IO.Path]::GetRelativePath($staging, $f.FullName)
  $final = Join-Path $Destination $rel
  if (Test-Path -LiteralPath $final) {
    $skipped++
    if ($PSCmdlet.ShouldProcess($f.FullName, "drop already-archived staged copy")) {
      Remove-Item -LiteralPath $f.FullName -Force
    }
    continue
  }
  if ($PSCmdlet.ShouldProcess($final, "publish staged replay")) {
    $parent = Split-Path -Parent $final
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Move-Item -LiteralPath $f.FullName -Destination $final
    $copied++
  }
}

# 3. Prune empty staging dirs left behind.
Get-ChildItem -LiteralPath $staging -Directory -Recurse |
  Sort-Object { $_.FullName.Length } -Descending |
  ForEach-Object { if (-not (Get-ChildItem -LiteralPath $_.FullName -Force | Select-Object -First 1)) { Remove-Item -LiteralPath $_.FullName -Force } }

Write-Output "sync complete: published=$copied already_archived=$skipped (robocopy exit=$rc)"
