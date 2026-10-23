<#
.SYNOPSIS
    Launches (or stops) the SchwabEngine Google Drive sync daemon as a hidden background process.

.DESCRIPTION
    Starts scripts/sync_engine_drive.py in daemon mode (15s poll, 2s debounce) with no console window.
    The daemon logs to logs/drive_sync.log and is single-instance: a second launch is refused.

.PARAMETER Stop
    Terminate every running sync_engine_drive.py daemon instead of starting one.

.PARAMETER Status
    Report whether the daemon is running and exit.

.PARAMETER Prune
    Pass --prune to the daemon (delete Drive files that no longer exist locally). Off by default.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start_drive_sync.ps1
    powershell -ExecutionPolicy Bypass -File scripts\start_drive_sync.ps1 -Status
    powershell -ExecutionPolicy Bypass -File scripts\start_drive_sync.ps1 -Stop
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Status,
    [switch]$Prune,
    [string]$DriveRoot = 'G:\My Drive\Projects\SchwabEngine'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SyncScript = Join-Path $PSScriptRoot 'sync_engine_drive.py'

function Get-SyncDaemons {
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*sync_engine_drive.py*' -and $_.CommandLine -notlike '*--once*' -and $_.ProcessId -ne $PID }
}

if ($Stop) {
    $daemons = @(Get-SyncDaemons)
    if ($daemons.Count -eq 0) { Write-Host 'No sync_engine_drive daemon is running.'; exit 0 }
    foreach ($d in $daemons) {
        Stop-Process -Id $d.ProcessId -Force
        Write-Host "Stopped sync daemon PID $($d.ProcessId)"
    }
    exit 0
}

$running = @(Get-SyncDaemons)
if ($Status) {
    if ($running.Count -gt 0) { Write-Host "Running: PID $($running[0].ProcessId)"; exit 0 }
    Write-Host 'Not running.'; exit 1
}
if ($running.Count -gt 0) {
    Write-Host "sync_engine_drive daemon already running (PID $($running[0].ProcessId)); nothing to do."
    exit 0
}

# Prefer the project venv's windowless interpreter, then pythonw on PATH, then python.
$candidates = @(
    (Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'),
    (Join-Path $ProjectRoot '.venv\Scripts\python.exe')
)
$python = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command python.exe -ErrorAction SilentlyContinue }
    if (-not $cmd) { throw 'No Python interpreter found (looked for .venv, pythonw, python).' }
    $python = $cmd.Source
}

$argList = @("`"$SyncScript`"", '--poll', '15', '--debounce', '2', '--drive-root', "`"$DriveRoot`"")
if ($Prune) { $argList += '--prune' }

# Launch through WMI so the daemon is created outside this shell's job object / process tree
# (it keeps running after the launching terminal or scheduled task exits) with a hidden window.
$daemonPid = $null
try {
    $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine              = "`"$python`" " + ($argList -join ' ')
        CurrentDirectory         = $ProjectRoot
        ProcessStartupInformation = $startup
    }
    if ($created.ReturnValue -eq 0) { $daemonPid = [int]$created.ProcessId }
} catch {
    Write-Warning "WMI launch failed ($($_.Exception.Message)); falling back to Start-Process."
}
if (-not $daemonPid) {
    $daemonPid = (Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru).Id
}

Start-Sleep -Seconds 2
if (-not (Get-Process -Id $daemonPid -ErrorAction SilentlyContinue)) {
    throw "Sync daemon exited immediately; inspect logs\drive_sync.log."
}
Write-Host "sync_engine_drive daemon started (PID $daemonPid) -> $DriveRoot"

