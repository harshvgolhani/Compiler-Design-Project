@echo off
setlocal
title SheetGuard - web app + backend
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
call tools\find_python.bat || exit /b 1
%PY% -c "import numpy, openpyxl" >nul 2>nul && goto :run
echo   Installing the optional packages numpy + openpyxl (one time) ...
%PY% -m pip install --quiet --disable-pip-version-check numpy openpyxl
if errorlevel 1 (
  echo   Could not install them - offline? SheetGuard still runs:
  echo   .xlsx upload/export and the NumPy back-end will be unavailable.
)
:run
echo.
echo   Starting SheetGuard on http://localhost:8765  (your browser will open)
echo   Keep this window open. Press Ctrl+C to stop.
echo.
%PY% -m sheetguard serve --port 8765
if errorlevel 1 (
  echo.
  echo   SheetGuard stopped with an error - run CHECK_SETUP.bat for a diagnosis.
)
pause
