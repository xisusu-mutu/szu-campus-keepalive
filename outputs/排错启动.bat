@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0campus_launcher.ps1" -Diagnose
set "launch_result=%errorlevel%"
echo.
echo You may close this diagnostic window. A running GUI will keep running.
echo Startup details: data\launcher.log
pause
exit /b %launch_result%
