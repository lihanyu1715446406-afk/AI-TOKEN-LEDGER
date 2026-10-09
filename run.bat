@echo off
rem ============================================================
rem  AI Token Ledger - Windows fallback launcher (CMD)
rem
rem  This file is deliberately ASCII-only. Mixing non-ASCII text
rem  with chcp / label scanning in a .bat is fragile and breaks in
rem  ways that are hard to diagnose. The PowerShell launcher
rem  (run.ps1) carries the localized messages instead.
rem
rem  chcp 65001 is safe here precisely BECAUSE the file is ASCII:
rem  it makes sync_tokens.py's UTF-8 Chinese output render correctly.
rem ============================================================
chcp 65001 >nul
title AI Token Ledger
setlocal
set "SCRIPT_DIR=%~dp0"
set "PYTHON="
set "PYARGS="

rem Try in order: py launcher -> python on PATH -> common install dirs.
rem No hardcoded version numbers.

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "PYTHON=py"
    set "PYARGS=-3"
)

if not defined PYTHON (
    where python >nul 2>nul
    if %ERRORLEVEL% EQU 0 set "PYTHON=python"
)

if not defined PYTHON (
    rem for /d enumerates in ascending name order, so the last hit wins (newest version)
    for %%R in ("%LOCALAPPDATA%\Programs\Python" "%ProgramFiles%" "C:\") do (
        for /d %%D in ("%%~R\Python3*") do (
            if exist "%%~fD\python.exe" set "PYTHON=%%~fD\python.exe"
        )
    )
)

echo ===========================================
echo   AI Token Ledger
echo ===========================================
echo.

if not defined PYTHON (
    echo ERROR: Python 3 not found.
    echo Install it from https://www.python.org/downloads/
    echo Remember to tick "Add Python to PATH" during setup.
    echo.
    pause
    exit /b 1
)

echo Python: %PYTHON% %PYARGS%
echo.
echo [1/2] Syncing data...
"%PYTHON%" %PYARGS% "%SCRIPT_DIR%sync_tokens.py"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ========== Sync failed ==========
    echo Python: %PYTHON% %PYARGS%
    echo Check that Python 3 is installed and on PATH.
    echo Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo.
echo [2/2] Opening dashboard...
start "" "%SCRIPT_DIR%dashboard.html"

echo.
echo Dashboard opened in your browser.
echo Run this file again to re-sync.
echo.
pause >nul
