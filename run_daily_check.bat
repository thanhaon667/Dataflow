@echo off
REM Daily check: sync ClickUp + list items needing attention
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m erp.daily_check
pause
