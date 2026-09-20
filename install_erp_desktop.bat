@echo off
REM Create the "ERP Desk" Desktop + Start Menu shortcuts (with the generated app icon).
REM Re-run any time; use  install_erp_desktop.bat /uninstall  to remove them again.
cd /d "%~dp0"
if /i "%~1"=="/uninstall" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "desktop\install_shortcut.ps1" -Uninstall
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "desktop\install_shortcut.ps1"
)
pause
