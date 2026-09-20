@echo off
REM Run the Script Center - single-page control room for every Python script
REM in this project (catalog, mindmap, editor, test runner, logs, daily digest).
REM Opens at http://localhost:8502 (a different port from the main dashboard
REM on 8501, so both can run at the same time).
REM
REM The --theme.* flags below apply this tool's brand palette/fonts only to
REM THIS launch - they are intentionally not stored in .streamlit\config.toml,
REM so dashboard\streamlit_app.py (started via run_dashboard.bat) keeps its
REM current look untouched.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python -m streamlit run dashboard/script_center.py --server.port 8502 ^
  --theme.base "light" ^
  --theme.primaryColor "#3452eb" ^
  --theme.backgroundColor "#f5f3ef" ^
  --theme.secondaryBackgroundColor "#ffffff" ^
  --theme.textColor "#1c1d22" ^
  --theme.borderColor "#e7e2d8" ^
  --theme.redColor "#e0393e" ^
  --theme.greenColor "#12b886" ^
  --theme.blueColor "#3452eb" ^
  --theme.violetColor "#7c5cff" ^
  --theme.orangeColor "#f2b705" ^
  --theme.baseRadius "16px" ^
  --theme.buttonRadius "12px" ^
  --theme.showWidgetBorder "true" ^
  --theme.font "'Public Sans':https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&display=swap" ^
  --theme.headingFont "'Fraunces':https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&display=swap" ^
  --theme.codeFont "'IBM Plex Mono':https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&display=swap"
pause
