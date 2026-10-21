@echo off
setlocal
title TMF Stopper

echo Stopping TMF backend...
if exist "%~dp0serving\scripts\stage2.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0serving\scripts\stage2.ps1" -Action stop
    if errorlevel 1 echo [WARN] Backend stop script reported an error.
) else (
    echo [WARN] Backend management script was not found.
)

echo Stopping TMF frontend window...
taskkill.exe /FI "WINDOWTITLE eq TMF Frontend*" /T /F >nul 2>nul
if errorlevel 1 (
    echo No TMF Frontend window was found, or it has already stopped.
) else (
    echo Frontend server stopped.
)
echo Stop operation finished.
pause
endlocal
