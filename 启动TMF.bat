@echo off
setlocal
title TMF Starter

rem LAN-first configuration. TMF_LAN_IPS can override detection with a comma-separated list.
if not defined TMF_FRONTEND_PORT set "TMF_FRONTEND_PORT=5173"
if not defined TMF_GATEWAY_PORT set "TMF_GATEWAY_PORT=8000"
if defined TMF_LAN_IP if not defined TMF_LAN_IPS set "TMF_LAN_IPS=%TMF_LAN_IP%"
set "TMF_LAN_IP="
set "TMF_CORS_ORIGINS="
for /f "tokens=1,2,3 delims=|" %%A in ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0serving\scripts\get_lan_ipv4.ps1" -FrontendPort %TMF_FRONTEND_PORT% -AddressList "%TMF_LAN_IPS%" 2^>nul') do (
    set "TMF_LAN_IP=%%A"
    set "TMF_LAN_IPS=%%B"
    set "TMF_CORS_ORIGINS=%%C"
)
if not defined TMF_LAN_IP (
    echo [ERROR] Could not determine this computer's LAN IPv4 addresses.
    echo Connect to Wi-Fi/Ethernet or set TMF_LAN_IPS to a comma-separated list of assigned IPv4 addresses.
    pause
    exit /b 1
)

rem Keep workers private on loopback; expose only Vite and the API gateway to LAN.
set "TMF_FRONTEND_HOST=0.0.0.0"
set "TMF_GATEWAY_HOST=0.0.0.0"
set "TMF_BIND_HOST=127.0.0.1"
set "TMF_WORKER_HOST=127.0.0.1"
set "VITE_API_BASE_URL="
set "VITE_API_PORT=%TMF_GATEWAY_PORT%"

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
echo Detected LAN IPv4 addresses: %TMF_LAN_IPS%
echo Frontend/API URLs:
for %%I in (%TMF_LAN_IPS:,= %) do echo   http://%%I:%TMF_FRONTEND_PORT%  ^(API http://%%I:%TMF_GATEWAY_PORT%^)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0serving\scripts\stage2.ps1" -Action start
if errorlevel 1 (
    echo [ERROR] Backend did not start successfully. Check serving\logs for details.
    pause
    exit /b 1
)

rem Vite is configured with strictPort, so a second instance would fail with
rem "Port <frontend port> is already in use". Reuse an already running TMF frontend instead.
set "TMF_FRONTEND_STATE=free"
for /f "delims=" %%I in ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0serving\scripts\check_frontend_lan.ps1" -Port %TMF_FRONTEND_PORT% -LanIPv4 "%TMF_LAN_IPS%" -GatewayPort %TMF_GATEWAY_PORT%') do set "TMF_FRONTEND_STATE=%%I"

if /i "%TMF_FRONTEND_STATE%"=="tmf" (
    echo TMF frontend is already running. Skipping a second Vite instance.
    echo Use the matching URL above on each network.
    echo Windows Defender Firewall must allow inbound TCP %TMF_FRONTEND_PORT% and %TMF_GATEWAY_PORT% on Private networks.
    pause
    endlocal
    exit /b 0
)
if /i "%TMF_FRONTEND_STATE%"=="other" (
    echo [ERROR] Port %TMF_FRONTEND_PORT% is occupied, or the existing TMF frontend is not bound for LAN access.
    echo Run 停止TMF.bat, then retry; unrelated processes using that port will not be stopped automatically.
    pause
    exit /b 1
)

echo Starting TMF frontend...
start "TMF Frontend" /D "%~dp0frontend" cmd.exe /k ""%TMF_PNPM%" dev"

set "TMF_FRONTEND_STATE=starting"
for /f "delims=" %%I in ('powershell.exe -NoProfile -Command "$deadline = (Get-Date).AddSeconds(30); $ready = $false; while ((Get-Date) -lt $deadline) { try { $r = Invoke-WebRequest -Uri http://127.0.0.1:%TMF_FRONTEND_PORT%/ -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop; if ($r.StatusCode -eq 200) { $ready = $true; break } } catch { }; Start-Sleep -Milliseconds 500 }; if ($ready) { Write-Output READY } else { Write-Output FAILED }"') do set "TMF_FRONTEND_STATE=%%I"
if /i not "%TMF_FRONTEND_STATE%"=="ready" (
    echo [ERROR] Frontend did not become ready within 30 seconds.
    echo Check the TMF Frontend window for pnpm/Vite errors. Backend services remain running.
    pause
    exit /b 1
)

echo Use the matching URL above on each network.
echo Ensure Windows Defender Firewall allows inbound TCP %TMF_FRONTEND_PORT% and %TMF_GATEWAY_PORT% on Private networks.
echo Worker ports 8101, 8103, 8104, 8105, and 8106 remain private on this computer.
echo Note: the backend starts the LLM worker; the frontend leaves LLM unchecked by default.
pause
endlocal
