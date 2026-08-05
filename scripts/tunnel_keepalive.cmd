@echo off
REM Keeps the public tunnel alive: reconnects automatically if it drops and
REM always requests the same address, so the URL stays stable:
REM   https://%IRADAR_TUNNEL_SUBDOMAIN%.loca.lt
REM Set IRADAR_TUNNEL_SUBDOMAIN to whatever name you want; any unclaimed one
REM works, and whoever runs this should pick their own rather than share it.
if "%IRADAR_TUNNEL_SUBDOMAIN%"=="" set IRADAR_TUNNEL_SUBDOMAIN=iradar-local
:loop
call npx --yes localtunnel --port 5000 --subdomain %IRADAR_TUNNEL_SUBDOMAIN%
timeout /t 5 /nobreak >nul
goto loop
