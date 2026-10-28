@echo off
rem Sets PY to a working Python 3.8+ command. Used by every launcher.
rem Prefers a stable release (3.13 ... 3.9) through the py launcher, because pre-release
rem Pythons (e.g. 3.15 beta) often have no numpy wheels. "python" may be the Store stub.
set "PY="
for %%V in (3.13 3.12 3.11 3.10 3.9 3.14) do (
  if not defined PY (
    py -%%V -c "import sys; sys.exit(0 if sys.version_info.releaselevel == 'final' else 1)" >nul 2>nul && set "PY=py -%%V"
  )
)
if defined PY goto :eof
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=py -3"
if defined PY goto :eof
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=python"
if defined PY goto :eof
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=python3"
if defined PY goto :eof
echo.
echo   Python 3.8 or newer was not found.
echo   Install Python 3.13 from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" in the installer, then run this again.
echo.
pause
exit /b 1
