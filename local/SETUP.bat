@echo off
cd /d "%~dp0.."
REM ============================================================
REM  iRadar - one-time setup. Run this ONCE.
REM  Needs Python 3.10+ installed (python.org, "Add to PATH" ticked).
REM ============================================================
cd /d "%~dp0"
echo.
echo [1/4] Creating isolated Python environment...
python -m venv .venv
if errorlevel 1 (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add Python to PATH" during installation, then run SETUP.bat again.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo.
echo [2/4] Installing libraries (1-2 minutes)...
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo.
echo [3/4] Downloading the browser engine (~150 MB, one time)...
python -m playwright install chromium
echo.
echo [4/4] Preparing the database...
python -c "import sys; sys.path.insert(0, 'iradar'); import db; db.init_db(); print('Database ready.')"
echo.
echo ============================================================
echo  Setup finished! Double-click START.bat to launch the tool.
echo ============================================================
pause
