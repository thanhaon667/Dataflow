@echo off
REM Smoke check = the MERGE GATE of the autonomous improvement loop (tests\smoke.py).
REM Read-only (SELECTs only, no ClickUp / DeepSeek / e-mail calls). About 22 seconds on an idle machine, but 45-55 on
REM a cold first run or while something else is using the disk - it is I/O bound, see README "Smoke check". Checks:
REM imports, PostgreSQL, Data Flow map sync, ERP Desk pages on a spare port, secret + stray-file scan, agent-system files + typography,
REM nothing left behind, branch discipline, and the gate's own self-test.
REM Exit code 0 only when every check passes (1 = a check failed, 2 = timed out, 3 = a --only subset passed).
REM Extra arguments are passed on:  run_smoke.bat --verbose    run_smoke.bat --only 3,5    run_smoke.bat --base main
REM
REM -B is required, not cosmetic: without it runpy compiles tests\__init__.py and tests\smoke.py to bytecode BEFORE
REM the module can set sys.dont_write_bytecode, so tests\__pycache__ appears in the work tree (lesson L-077).
REM
REM This window NEVER waits for a key unless you set SMOKE_PAUSE (for example `set SMOKE_PAUSE=1` before a
REM double-click, or create a shortcut that sets it). There is deliberately no "was it double-clicked?" guess:
REM %cmdcmdline% looks identical for Explorer, `cmd /c`, PowerShell and Task Scheduler, so such a test either
REM hangs unattended jobs or never fires at all (lessons L-078 and L-013). Double-clicking without SMOKE_PAUSE
REM therefore runs the gate and closes the window; run it from a terminal to read the table.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -B -m tests.smoke %*
set SMOKE_RC=%ERRORLEVEL%
if defined SMOKE_PAUSE pause
exit /b %SMOKE_RC%
