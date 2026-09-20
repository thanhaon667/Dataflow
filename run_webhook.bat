@echo off
REM Run the lead-receiving webhook - keep this window open while it's running (Ctrl+C to stop)
cd /d "%~dp0"
call venv\Scripts\activate.bat
echo.
echo Webhook is running at http://127.0.0.1:8000/webhooks/leads
echo Press Ctrl+C to stop.
echo.
python -m uvicorn erp.webhook_app:app --host 127.0.0.1 --port 8000
pause
