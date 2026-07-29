@echo off
REM Keeps the public tunnel alive: reconnects automatically if it drops and
REM always requests the same address, so the URL stays stable:
REM   https://infineonwatch-anas.loca.lt
:loop
call npx --yes localtunnel --port 5000 --subdomain infineonwatch-anas
timeout /t 5 /nobreak >nul
goto loop
