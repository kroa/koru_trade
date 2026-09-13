' ============================================================
'  Launch the KORU watcher hidden, in the background.
' ============================================================
'  Keep this file ASCII-only (same codepage reason as the .bat).
'
'  To make the watcher survive a reboot, put a copy or a
'  shortcut of this file in the Startup folder:
'      Win+R  ->  shell:startup
'
'  Why: on 2026-08-31 a reboot killed the watcher and nobody
'  noticed for 25 hours. A dead watcher and "no signal today"
'  look exactly the same from the phone.
'
'  Run args: 0 = hidden window, False = do not wait.
' ============================================================

Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c ""C:\Koru_Trade\scripts\run_watch.bat""", 0, False
