@echo off
REM Launch ERP Desk - the local "desktop app" (live Reporting + Management in one
REM window). Starts hidden with no console (pythonw), so this window closes at once.
REM Launching again while it is already running just brings its window to the front.
REM Logs: erp_desktop.log and erp_desktop_streamlit.log in this folder.
REM Debug with visible logs instead: run_erp_desktop_debug.bat
cd /d "%~dp0"
start "" "venv\Scripts\pythonw.exe" -m desktop
