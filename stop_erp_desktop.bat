@echo off
REM Ask a running ERP Desk to quit (same as the Quit button inside the app):
REM stops the window, the live report feed and the Script Center.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m desktop --stop
