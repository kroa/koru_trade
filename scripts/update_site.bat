@echo off
REM ============================================================
REM  KORU public site updater
REM ============================================================
REM  NOTE: keep this file ASCII-only.
REM  cmd.exe reads .bat in the OEM codepage (CP949 on Korean
REM  Windows). UTF-8 Korean comments get mangled into garbage
REM  and cmd tries to execute them as commands.
REM
REM  What it does: rebuild docs/ from fresh quotes, then commit
REM  and push. GitHub Pages redeploys on its own after the push.
REM
REM  build_public.py refuses to publish when the newest bar is
REM  OLDER than the one already published. That happens for real:
REM  on 2026-09-24 the provider served the 09-22 bar as newest
REM  while 09-23 was still consolidating. A silent republish
REM  would have moved the public page a day backwards.
REM  So a non-zero exit here is a normal outcome, not a failure
REM  to retry blindly - the next run picks it up.
REM
REM  Register (run as the same user, from an elevated prompt):
REM    schtasks /create /tn "KORU site" /tr "C:\Koru_Trade\scripts\update_site.bat" ^
REM             /sc daily /st 11:40 /f
REM  Remove:
REM    schtasks /delete /tn "KORU site" /f
REM ============================================================

cd /d "%~dp0.."

set "PYTHONPATH=%CD%\src"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist "logs" mkdir "logs"
set "LOG=logs\site.log"

echo. >> "%LOG%"
echo [%DATE% %TIME%] update start >> "%LOG%"

".venv\Scripts\python.exe" scripts\build_public.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%DATE% %TIME%] build refused or failed - nothing published >> "%LOG%"
    exit /b 1
)

git diff --quiet -- docs
if errorlevel 1 (
    git add docs
    git commit -m "chore: 사이트 갱신" >> "%LOG%" 2>&1
    git push origin main >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo [%DATE% %TIME%] push failed >> "%LOG%"
        exit /b 1
    )
    echo [%DATE% %TIME%] published >> "%LOG%"
) else (
    echo [%DATE% %TIME%] no change - skipped >> "%LOG%"
)

exit /b 0
