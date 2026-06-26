# Build the portable Windows .exe. Run from the repo root with the venv active:
#   .venv\Scripts\Activate.ps1
#   .\build_exe.ps1            # or  .\build_exe.ps1 -Clean
#
# Output: dist\horizon-monitor.exe  (single-file, windowed). See horizon-monitor.spec.
param([switch]$Clean)
$ErrorActionPreference = "Stop"

python -m pip install --quiet pyinstaller
$pyiArgs = @("--noconfirm", "horizon-monitor.spec")
if ($Clean) { $pyiArgs = @("--clean") + $pyiArgs }
python -m PyInstaller @pyiArgs

Write-Host "`nBuilt dist\horizon-monitor.exe" -ForegroundColor Green
Write-Host "Drop a .env (ANTHROPIC_API_KEY, optionally VOYAGE_API_KEY / HORIZON_PASSWORD /"
Write-Host "TELEGRAM_*) beside the .exe, or set them in the app's Settings tab."
