@echo off
REM ---------------------------------------------------------------------------
REM PM2 boot recovery — restores the saved pm2 process list after a reboot.
REM Invoked by the "PM2 Resurrect" Scheduled Task (trigger: At startup).
REM
REM 2026-08-07: this file previously contained six LITERAL "<user>" placeholders
REM that were never substituted, so every path resolved to a non-existent
REM directory and the task failed on every single boot. Reboot recovery had
REM never actually worked. Do not re-introduce placeholders — this script must
REM stay directly executable.
REM
REM Logs to pm2-resurrect.log next to this script, so a failed boot leaves a
REM trace instead of vanishing: the task runs with no visible console, which is
REM precisely how the broken version stayed invisible.
REM ---------------------------------------------------------------------------

set "PM2_USER=YOUR_USERNAME"
set "USERPROFILE=C:\Users\%PM2_USER%"
set "APPDATA=C:\Users\%PM2_USER%\AppData\Roaming"
set "PM2_HOME=C:\Users\%PM2_USER%\.pm2"
set "PATH=C:\Program Files\nodejs;C:\Users\%PM2_USER%\AppData\Roaming\npm;%PATH%"

set "LOG=%~dp0pm2-resurrect.log"

echo. >> "%LOG%"
echo ==== pm2 resurrect at %DATE% %TIME% ==== >> "%LOG%"

cd /d "C:\Users\%PM2_USER%\Projects\AI-team"

call "C:\Users\%PM2_USER%\AppData\Roaming\npm\pm2.cmd" resurrect >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"

echo pm2 resurrect exited with %RC% >> "%LOG%"
exit /b %RC%
