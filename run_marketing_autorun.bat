@echo off
REM Process the marketing inbox once: every CSV in data_inbox\incoming (or MARKETING_INBOX in .env) is detected, loaded
REM through the marketing pipeline, moved to processed\ or failed\ (next to the inbox folder), and the rollup is refreshed once.
REM Extra arguments are passed through:  run_marketing_autorun.bat --dry-run
REM                                       run_marketing_autorun.bat --inbox D:\exports\marketing
REM Log: data_inbox\marketing_autorun.log (git-ignored). NOT scheduled by anything today - registering it in Windows Task
REM Scheduler is the owner's decision (see docs/marketing-data-architecture.md).
REM
REM Intentionally NO "pause" at the end: this file is meant to run unattended under Task Scheduler, where pause would wait
REM forever for a keypress nobody gives. Exit code: 0 nothing failed, 1 a file / the database / the rollup failed, 2 bad argument.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m erp.marketing.autorun %*
exit /b %ERRORLEVEL%
