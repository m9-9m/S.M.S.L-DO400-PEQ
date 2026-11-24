@echo off
setlocal
title DO400 DFU Update Tool (XMOS / Thesycon API)

rem ------------------------------------------------------------------
rem  DO400 DFU update app launcher
rem    (no argument)          : open the GUI
rem    --check-device         : read-only device status (no write)
rem    --list-devices         : list audio devices  (read-only)
rem    --validate X.bin       : offline image validation (no device)
rem    --dry-run-flash X.bin  : dry-run of the DFU write sequence
rem
rem  NOTE: this .bat is intentionally pure ASCII so that it parses
rem        correctly under any console code page. The Japanese UI and
rem        CLI text are printed by Python itself.
rem ------------------------------------------------------------------

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python was not found in PATH.
    pause
    exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.8 or newer is required.
    python --version
    pause
    exit /b 1
)

python "%~dp0do400_dfu_app.py" %*
set RC=%ERRORLEVEL%

if not "%~1"=="" (
    echo.
    echo [exit code %RC%]
    pause
)

endlocal & exit /b %RC%
