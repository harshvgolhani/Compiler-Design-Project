@echo off
setlocal
title SheetGuard - public tunnel for Google Sheets
cd /d "%~dp0"
echo.
echo   Google Sheets runs in Google's cloud, so it needs a PUBLIC https URL
echo   for your local SheetGuard server. This uses a free Cloudflare quick tunnel
echo   (no account needed). Start START_SheetGuard.bat first!
echo.
set "CF="
where cloudflared >nul 2>nul && set "CF=cloudflared"
if not defined CF if exist "%~dp0cloudflared.exe" set "CF=%~dp0cloudflared.exe"
if defined CF goto :run
echo   cloudflared is not installed. Install it with ONE of:
echo     winget install --id Cloudflare.cloudflared
echo   or download cloudflared-windows-amd64.exe from
echo     https://github.com/cloudflare/cloudflared/releases/latest
echo   rename it to cloudflared.exe and put it in this folder.
echo.
pause
exit /b 1
:run
echo   Look for a line like   https://something-random.trycloudflare.com
echo   and paste that URL into the SheetGuard sidebar settings (gear icon).
echo.
"%CF%" tunnel --url http://localhost:8765
pause
