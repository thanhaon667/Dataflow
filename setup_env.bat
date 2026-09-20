@echo off
REM =========================================================================
REM  PYTHON ENVIRONMENT SETUP - Data Processing Analyst
REM  Run this file by double-clicking it (or right-click > Run)
REM =========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   PYTHON ENVIRONMENT SETUP
echo ============================================
echo.

REM --- Check whether Python is installed ---
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python was not found on this machine.
    echo.
    echo Please install Python first from: https://www.python.org/downloads/
    echo IMPORTANT: when installing, make sure to CHECK "Add python.exe to PATH"
    echo Once installed, run this setup_env.bat file again.
    echo.
    pause
    exit /b 1
)

echo Found Python:
python --version
echo.

REM --- Create a virtual environment named "venv" right here ---
if exist venv (
    echo The "venv" folder already exists, skipping creation.
) else (
    echo Creating virtual environment "venv" ...
    python -m venv venv
)
echo.

REM --- Activate venv and install dependencies from requirements.txt ---
echo Activating venv and installing required packages...
call venv\Scripts\activate.bat

python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo ============================================
echo   DONE!
echo ============================================
echo To use this environment later:
echo   1. Open Command Prompt (cmd) in this folder
echo   2. Run:  venv\Scripts\activate
echo   3. Then run a script, e.g.:  python -m erp.daily_check
echo.
pause
