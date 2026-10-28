@echo off
setlocal
title SheetGuard - installation check
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
call tools\find_python.bat || exit /b 1
%PY% -m sheetguard doctor
echo.
%PY% -m unittest discover -s tests
pause
