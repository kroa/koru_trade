@echo off
REM ============================================================
REM  KORU watcher launcher (alerts only)
REM ============================================================
REM  NOTE: keep this file ASCII-only.
REM  cmd.exe reads .bat in the OEM codepage (CP949 on Korean
REM  Windows). UTF-8 Korean comments get mangled into garbage
REM  and cmd tries to execute them as commands. Learned the
REM  hard way on 2026-09-02.
REM
REM  Why this file exists:
REM    Running the watcher straight from a terminal means it
REM    dies with the terminal and does not survive a reboot.
REM    That happened on 2026-08-31 and went unnoticed for 25
REM    hours. A dead watcher and "no signal" look identical
REM    from the outside, so this logs and can be auto-started.
REM
REM  PYTHONPATH is required: the package is NOT pip-installed
REM  into .venv, so -m koru_trade fails without it.
REM ============================================================

cd /d "%~dp0.."

set "PYTHONPATH=%CD%\src"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist "logs" mkdir "logs"

echo [%DATE% %TIME%] watcher starting >> "logs\watch.log"

".venv\Scripts\python.exe" -u -m koru_trade --log-level INFO watch --paper --interval 900 --no-cache --state "state\koru_state.db" >> "logs\watch.log" 2>&1

echo [%DATE% %TIME%] watcher exited with %ERRORLEVEL% >> "logs\watch.log"
