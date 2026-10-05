@echo off
REM ============================================================
REM  KORU pre-market checker & briefing
REM ============================================================
REM  NOTE: keep this file ASCII-only with CRLF line endings.
REM
REM  What it does: check pre-market quotes, verdict, box-range,
REM  and generate Gemini briefing before US market open.
REM
REM  Register (run as the same user, from an elevated prompt):
REM    schtasks /create /tn "KORU pre-market" /tr "C:\Koru_Trade\scripts\check_market.bat" /sc daily /st 21:40 /f
REM  Remove:
REM    schtasks /delete /tn "KORU pre-market" /f
REM ============================================================

cd /d "%~dp0.."

set "PYTHONPATH=%CD%\src;%CD%\scripts"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist "logs" mkdir "logs"
set "LOG=logs\check_market.log"

echo. >> "%LOG%"
echo [%DATE% %TIME%] pre-market check start >> "%LOG%"

".venv\Scripts\python.exe" scripts\check_market.py --notify --mode premarket >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%DATE% %TIME%] check failed >> "%LOG%"
    exit /b 1
)

echo [%DATE% %TIME%] check completed successfully >> "%LOG%"
exit /b 0
