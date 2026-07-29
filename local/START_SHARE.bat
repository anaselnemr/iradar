@echo off
cd /d "%~dp0.."
REM ============================================================
REM  iRadar - launch + temporary public link.
REM  Starts the app AND creates a temporary internet URL so you
REM  can open the dashboard from another laptop/phone browser.
REM
REM  1) A line "your url is: https://xxxx.loca.lt" will appear.
REM     Open that link on the other device.
REM  2) First visit shows a "tunnel password" page: the password is
REM     this PC's public IP - it opens automatically in your browser
REM     (https://loca.lt/mytunnelpassword). Type it once.
REM
REM  The link changes every time you run this. Anyone who has BOTH
REM  the link and the password can view/change your dashboard while
REM  this window is open - share carefully, close when done.
REM ============================================================
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Please run SETUP.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
start "iRadar server" cmd /k python iradarpp.py
timeout /t 4 /nobreak >nul
start "" https://loca.lt/mytunnelpassword
echo Your stable link: https://infineonwatch-anas.loca.lt
call scripts\tunnel_keepalive.cmd
