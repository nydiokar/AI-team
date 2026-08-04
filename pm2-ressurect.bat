@echo off

set USERPROFILE=C:\Users\<user>
set APPDATA=C:\Users\<user>\AppData\Roaming
set PM2_HOME=C:\Users\<user>\.pm2
set PATH=C:\Program Files\nodejs;C:\Users\<user>\AppData\Roaming\npm;%PATH%

cd /d C:\Users\<user>\Projects\AI-team

call "C:\Users\<user>\AppData\Roaming\npm\pm2.cmd" resurrect

exit /b %ERRORLEVEL%
