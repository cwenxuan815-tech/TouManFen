@echo off
setlocal
title TMF Stopper

rem Stop only the tracked stage2 processes and the dedicated TMF Vite window.
rem Do not terminate unrelated Python/Node processes.
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
echo Checking whether TMF ports have been released...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0serving\scripts\check_tmf_ports.ps1"
if errorlevel 1 (
    echo [WARN] One or more TMF ports still have listeners. See port/PID details above; this script does not kill unowned processes.
) else (
    echo TMF frontend and gateway are stopped; phone access is unavailable until 启动TMF.bat runs again.
)
pause
endlocal
