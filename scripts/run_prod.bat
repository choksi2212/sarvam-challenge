@echo off
rem ============================================================================
rem BhashaBuzzer ops - start the health watchdog in the background (Windows).
rem
rem   Usage :  scripts\run_prod.bat [extra watchdog args, e.g. --interval 10]
rem   Log   :  scripts\watchdog.log   (first line contains the watchdog PID)
rem   Stop  :  taskkill /F /PID ^<pid from the first log line^>
rem
rem pythonw runs the watchdog with NO console window, detached from (and
rem surviving) this cmd window. Falls back to python if pythonw is not found.
rem ============================================================================
setlocal
set "HERE=%~dp0"
set "PYW=pythonw"
where pythonw >nul 2>nul || set "PYW=python"

start "bhasha-watchdog" /min %PYW% "%HERE%watchdog.py" %*

echo Watchdog started via %PYW% (background, detached from this window).
echo Log file : %HERE%watchdog.log
echo To stop  : open the log, note the pid= on the first line, then: taskkill /F /PID ^<pid^>
endlocal
