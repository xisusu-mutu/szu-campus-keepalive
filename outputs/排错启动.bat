@echo off
setlocal
cd /d "%~dp0"
if exist "D:\Anaconda\python.exe" (
    "D:\Anaconda\python.exe" "%~dp0campus_keepalive.py"
) else (
    py -3 "%~dp0campus_keepalive.py"
)
echo.
echo Application exited. Any error details are shown above.
pause
