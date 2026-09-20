@echo off
REM Run ERP Desk with a console window that shows the log live (for troubleshooting).
REM Closing the app window - or pressing Ctrl+C here - stops everything.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m desktop --console
pause
