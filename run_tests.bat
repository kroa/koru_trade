@echo off
REM ============================================================
REM  KORU test & verification runner
REM ============================================================
REM  Runs scripts\verify.py with all checks (format, lint, types, tests, secrets)
REM  Usage:
REM    run_tests.bat        (full verification)
REM    run_tests.bat --fast (quick tests without type/cov)

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

".venv\Scripts\python.exe" scripts\verify.py %*
