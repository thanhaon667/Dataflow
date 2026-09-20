@echo off
REM Generate the daily digest (leads/tickets metrics, deltas vs. yesterday,
REM AI narrative) - emails it (if SMTP is configured in .env) and writes
REM daily_digest_latest.json for the Script Center page to display.
REM Schedule this with Windows Task Scheduler to run once every morning.
REM
REM Intentionally NO "pause" at the end: this file is meant to run unattended
REM under Task Scheduler ("Run whether user is logged on or not"), and pause
REM would sit forever waiting for a keypress nobody is there to give, so the
REM task would never finish (and the "email me on error" alert - see
REM erp/daily_digest.py's own try/except around run_daily_digest() - would
REM never even get a chance to fire). If you run this by double-clicking and
REM want to read the console output before the window closes, run it from an
REM already-open terminal instead: run_daily_digest.bat
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m erp.daily_digest
exit /b %ERRORLEVEL%
