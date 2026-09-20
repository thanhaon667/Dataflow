@echo off
REM Run the dashboard to view data - opens automatically at http://localhost:8501
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m streamlit run dashboard/streamlit_app.py
pause
