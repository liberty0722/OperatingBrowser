@echo off
echo Stopping AI Browser Controller...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000" ^| findstr "LISTENING"') do (
    echo Killing process PID: %%a
    taskkill /F /PID %%a >nul 2>&1
)
echo Done.
pause
