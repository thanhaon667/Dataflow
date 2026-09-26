@echo off
REM Refresh the marketing daily rollup (interaction_daily_rollup), pre-create the next
REM monthly interaction_fact partitions, and report (never drop) partitions past retention.
REM Extra arguments are passed through:  run_marketing_rollup.bat --days 7
REM                                       run_marketing_rollup.bat --full --yes
REM Log: marketing_rollup.log next to this file. NOT scheduled by anything today - registering it
REM in Windows Task Scheduler is the owner's decision (see docs/marketing-data-architecture.md).
REM
REM Intentionally NO "pause" at the end: this file is meant to run unattended under Task Scheduler
REM ("Run whether user is logged on or not"), where pause would wait forever for a keypress nobody
REM gives. The exit code is 0 when every step finished, 1 when a step failed, 2 for a bad argument.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m erp.marketing.rollup %*
exit /b %ERRORLEVEL%
