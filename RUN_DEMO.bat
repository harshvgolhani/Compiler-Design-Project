@echo off
setlocal
title SheetGuard - compiler demo (terminal)
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
call tools\find_python.bat || exit /b 1
echo ============================================================
echo   SheetGuard  -  Compiler for spreadsheet formulas
echo ============================================================
echo.
echo [0] Installation check (all engines self-test):
%PY% -m sheetguard doctor
echo.
echo [1] Lexer:  =SUM(Sales!B2:B9)*(1+TaxRate)
%PY% -m sheetguard lex "=SUM(Sales!B2:B9)*(1+TaxRate)"
echo.
echo [2] Parser / AST:
%PY% -m sheetguard parse "=SUM(Sales!B2:B9)*(1+TaxRate)"
echo.
echo [3] Syntax error reporting:  =SUM(A1:A3
%PY% -m sheetguard parse "=SUM(A1:A3"
echo.
echo [4] Whole-workbook analysis of the demo sheet:
%PY% -m sheetguard analyze -v -t
echo.
echo [5] Auto-fix + annotated Excel copy:
%PY% -m sheetguard fix -o demo\Acme_Q3_Sales_FIXED.xlsx
echo.
echo [6] Generated code for one output cell (program slice):
%PY% -m sheetguard compile --target "Summary!B6"
echo.
echo [7] Benchmark - interpreter vs compiled vs NumPy:
%PY% -m sheetguard bench --rows 2000
echo.
echo [8] Unit tests:
%PY% -m unittest discover -s tests
echo.
pause
