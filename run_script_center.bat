@echo off
REM Run the Script Center - single-page control room for every Python script
REM in this project (catalog, mindmap, editor, test runner, logs, daily digest).
REM Opens at http://localhost:8502. ERP Desk embeds the same page as its
REM Management tab and starts it on 47651, so the two can run side by side.
REM
REM The font flags are a literal copy of erp/typography.py (Montserrat; a system monospace for code only): a .bat
REM cannot import Python, so change the font in both places (tests\smoke.py checks that they still agree).
REM The --theme.* flags below apply this tool's brand palette/fonts only to
REM THIS launch - they are intentionally not stored in .streamlit\config.toml,
REM which would be picked up by every Streamlit app started from this folder.
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
  --theme.font "'Montserrat':https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700&display=swap" ^
  --theme.headingFont "'Montserrat':https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700&display=swap" ^
  --theme.codeFont "Consolas, 'Cascadia Mono', 'Courier New', monospace"
pause
