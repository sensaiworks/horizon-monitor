# Emergency Stop -- kill every horizon-monitor-related process on THIS machine,
# right now, without needing Claude Code to see or act on a message first.
#
# WHY THIS EXISTS: typing "stop" in chat is a request Claude has to receive and
# act on -- it is not instant, and if a batch pull is already running as a
# background process, there can be real, dangerous delay before it's actually
# killed (confirmed live: Claude's own TaskStop tool reported success while the
# underlying process kept running for another minute). This script needs no
# cooperation from Claude's current turn at all -- it goes straight to Windows
# and kills the processes directly.
#
# Matches by COMMAND LINE, not by broad process name, so it won't touch your
# other, unrelated Python/Node/Git-Bash work: only processes that mention
# horizon-monitor, horizon-mcp, or main.py specifically.
#
# Usage: double-click this file (if .ps1 is associated) or run from a terminal:
#   powershell -ExecutionPolicy Bypass -File "Emergency Stop.ps1"

$ErrorActionPreference = "Continue"

Write-Host "Scanning for horizon-monitor-related processes..." -ForegroundColor Cyan

$targets = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match 'python|node|bash') -and
    ($_.CommandLine -match 'horizon-monitor|horizon-mcp|main\.py')
}

if (-not $targets) {
    Write-Host "Nothing found running. Already clear." -ForegroundColor Green
    exit 0
}

Write-Host "`nKilling $($targets.Count) process(es):" -ForegroundColor Yellow
foreach ($p in $targets) {
    $shortCmd = if ($p.CommandLine.Length -gt 100) { $p.CommandLine.Substring(0, 100) + "..." } else { $p.CommandLine }
    Write-Host "  PID $($p.ProcessId): $shortCmd"
}

$targets | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Milliseconds 500

$remaining = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match 'python|node|bash') -and
    ($_.CommandLine -match 'horizon-monitor|horizon-mcp|main\.py')
}

if ($remaining) {
    Write-Host "`nWARNING: $($remaining.Count) process(es) survived the kill. Try running this again, or check Task Manager." -ForegroundColor Red
    $remaining | Select-Object ProcessId, Name | Format-Table
} else {
    Write-Host "`nAll clear. Nothing horizon-monitor-related is running anymore." -ForegroundColor Green
}

Write-Host "`nNote: this stops LOCAL automation only. It does not touch the VDI/Horizon" -ForegroundColor Cyan
Write-Host "session itself -- whatever was mid-action there when this ran stays as-is." -ForegroundColor Cyan
