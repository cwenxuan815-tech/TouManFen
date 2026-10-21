@echo off
setlocal
title TMF Starter

if not exist "%~dp0serving\scripts\stage2.ps1" (
    echo [ERROR] Backend startup script was not found.
    pause
    exit /b 1
)
if not exist "%~dp0frontend\package.json" (
    echo [ERROR] Frontend folder was not found.
    pause
    exit /b 1
)
set "TMF_PNPM="
for /f "delims=" %%I in ('where pnpm 2^>nul') do if not defined TMF_PNPM set "TMF_PNPM=%%I"

rem Codex desktop may provide Node.js/pnpm in its bundled runtime without adding
rem them to the PATH inherited by a double-clicked batch file.
if not defined TMF_PNPM if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd" (
    set "TMF_PNPM=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd"
    set "PATH=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin;%PATH%"
)

if not defined TMF_PNPM (
    echo [ERROR] pnpm was not found.
    echo Install Node.js and pnpm, or run this script from the configured Codex desktop environment.
    pause
    exit /b 1
)
if not exist "%~dp0frontend\node_modules" (
    echo [ERROR] Frontend dependencies are missing.
    echo Run these commands once:
    echo   cd frontend
    echo   pnpm install
    pause
    exit /b 1
)

echo Starting TMF backend. Please wait until all services report ready.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0serving\scripts\stage2.ps1" -Action start
if errorlevel 1 (
    echo [ERROR] Backend did not start successfully. Check serving\logs for details.
    pause
    exit /b 1
)

rem Vite is configured with strictPort, so a second instance would fail with
rem "Port 5173 is already in use". Reuse an already running TMF frontend instead.
set "TMF_FRONTEND_STATE=free"
for /f "delims=" %%I in ('powershell.exe -NoProfile -Command "try { $r = Invoke-WebRequest -Uri http://192.168.12.54:5173/ -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop; if ($r.StatusCode -eq 200) { Write-Output TMF } else { Write-Output OTHER } } catch { if (Get-NetTCPConnection -State Listen -LocalPort 5173 -ErrorAction SilentlyContinue) { Write-Output OTHER } else { Write-Output FREE } }"') do set "TMF_FRONTEND_STATE=%%I"

if /i "%TMF_FRONTEND_STATE%"=="tmf" (
    echo TMF frontend is already running. Skipping a second Vite instance.
    echo Open http://192.168.12.54:5173 in your browser.
    echo LAN members can open the same URL ^(ensure Windows Firewall allows 5173/8000^).
    pause
    endlocal
    exit /b 0
)
if /i "%TMF_FRONTEND_STATE%"=="other" (
    echo [ERROR] Port 5173 is occupied by another process, so the TMF frontend cannot start.
    echo Close that process or run the TMF stop script, then retry.
    pause
    exit /b 1
)

echo Starting TMF frontend...
start "TMF Frontend" /D "%~dp0frontend" cmd.exe /k ""%TMF_PNPM%" dev"

echo Open http://192.168.12.54:5173 in your browser.
echo LAN members can open the same URL (ensure Windows Firewall allows 5173/8000).
echo Note: the backend starts the LLM worker; the frontend leaves LLM unchecked by default.
pause
endlocal
