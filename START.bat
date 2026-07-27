@echo off
REM ============================================================
REM  iRadar - launch. Keep this window open while using.
REM ============================================================
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Please run SETUP.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
start "" http://127.0.0.1:5000
python app.py
pause
