@echo off

set USERPROFILE=C:\Users\Cicada38
set APPDATA=C:\Users\Cicada38\AppData\Roaming
set PM2_HOME=C:\Users\Cicada38\.pm2
set PATH=C:\Program Files\nodejs;C:\Users\Cicada38\AppData\Roaming\npm;%PATH%

cd /d C:\Users\Cicada38\Projects\AI-team

call "C:\Users\Cicada38\AppData\Roaming\npm\pm2.cmd" resurrect

exit /b %ERRORLEVEL%