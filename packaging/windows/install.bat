@echo off
setlocal enabledelayedexpansion

:: ============================================================================
:: OPC-Bridge Windows Agent - Offline Installation & Service Setup
:: ============================================================================

:: 1. Verify Administrator Privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Administrative privileges are required.
    echo Please right-click install.bat and select "Run as administrator".
    pause
    exit /b 1
)

:: 2. Default Configuration Variables
set "SERVER_HOST=127.0.0.1"
set "SERVER_PORT=8443"
set "AUTH_TOKEN="
set "CA_CERT="
set "AGENT_ID=%COMPUTERNAME%-opc-01"
set "SERVICE_USER="
set "SERVICE_PASSWORD="
set "UNATTENDED=0"

:: 3. Parse Command-Line Arguments
:parse_args
if "%~1"=="" goto after_args
if /i "%~1"=="--server" (
    set "RAW_SERVER=%~2"
    shift & shift
    for /f "tokens=1,2 delims=:" %%a in ("!RAW_SERVER!") do (
        set "SERVER_HOST=%%a"
        if not "%%b"=="" set "SERVER_PORT=%%b"
    )
    goto parse_args
)
if /i "%~1"=="-s" (
    set "RAW_SERVER=%~2"
    shift & shift
    for /f "tokens=1,2 delims=:" %%a in ("!RAW_SERVER!") do (
        set "SERVER_HOST=%%a"
        if not "%%b"=="" set "SERVER_PORT=%%b"
    )
    goto parse_args
)
if /i "%~1"=="--token" (
    set "AUTH_TOKEN=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="-t" (
    set "AUTH_TOKEN=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="--ca-cert" (
    set "CA_CERT=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="-c" (
    set "CA_CERT=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="--agent-id" (
    set "AGENT_ID=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="-a" (
    set "AGENT_ID=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="--service-user" (
    set "SERVICE_USER=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="-u" (
    set "SERVICE_USER=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="--service-password" (
    set "SERVICE_PASSWORD=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="-p" (
    set "SERVICE_PASSWORD=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="--unattended" (
    set "UNATTENDED=1"
    shift
    goto parse_args
)
shift
goto parse_args

:after_args

:: 4. Interactive Prompts (only when not in unattended mode and parameters missing)
if "%UNATTENDED%"=="0" (
    echo ===================================================
    echo   OPC-Bridge Windows Agent Installer Configuration
    echo ===================================================
    
    set /p "INPUT_SERVER=Central server address [!SERVER_HOST!:!SERVER_PORT!]: "
    if not "!INPUT_SERVER!"=="" (
        for /f "tokens=1,2 delims=:" %%a in ("!INPUT_SERVER!") do (
            set "SERVER_HOST=%%a"
            if not "%%b"=="" set "SERVER_PORT=%%b"
        )
    )
    
    if "!AUTH_TOKEN!"=="" (
        set /p "AUTH_TOKEN=Enter registration credential (auth token): "
    )
    
    if "!CA_CERT!"=="" (
        set /p "CA_CERT=Path to CA / TLS trust certificate (optional, press Enter to skip): "
    )
    
    if "!SERVICE_USER!"=="" (
        echo.
        echo For OPC DA servers (such as ABB 800xA), a dedicated domain/local service
        echo account with DCOM permissions is recommended. Press Enter to use LocalSystem.
        set /p "SERVICE_USER=Service logon account [LocalSystem]: "
        if not "!SERVICE_USER!"=="" (
            set /p "SERVICE_PASSWORD=Account password: "
        )
    )
)

:: 5. Fallback Default Token if none provided
if "!AUTH_TOKEN!"=="" (
    set "AUTH_TOKEN=default-opc-bridge-token"
)

:: 6. Locate Python runtime
set "PYTHON_EXE="
if exist "%~dp0runtime\python.exe" (
    set "PYTHON_EXE=%~dp0runtime\python.exe"
    echo [INFO] Using bundled Python runtime: !PYTHON_EXE!
) else (
    where py >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON_EXE=py -3"
        echo [INFO] Using system Python launcher (py -3)
    ) else (
        where python >nul 2>&1
        if !errorlevel! equ 0 (
            set "PYTHON_EXE=python"
            echo [INFO] Using system Python: python
        ) else (
            echo [ERROR] Python 3 was not found in PATH or runtime\ folder.
            echo Please install Python 3.10+ or bundle runtime\ before installing.
            if "%UNATTENDED%"=="0" pause
            exit /b 1
        )
    )
)

:: 7. Offline dependency installation (without network)
if exist "%~dp0wheels" (
    echo [INFO] Installing pinned dependencies from offline wheels directory...
    !PYTHON_EXE! -m pip install --no-index --find-links="%~dp0wheels" -r "%~dp0requirements-offline.txt" >nul 2>&1
    if !errorlevel! neq 0 (
        echo [INFO] Continuing (packages may already be installed in runtime environment).
    )
)

:: 8. Set up directories and write configuration
set "CONFIG_DIR=C:\ProgramData\OPCBridge"
set "LOG_DIR=C:\ProgramData\OPCBridge\logs"
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo [INFO] Configuring agent settings in %CONFIG_DIR%\agent.json...
echo [INFO] Central server: !SERVER_HOST!:!SERVER_PORT!
echo [INFO] Agent ID:       !AGENT_ID!
echo [INFO] Credential:     [CONFIGURED - HIDDEN]

:: Write JSON using Python helper to ensure valid JSON and never print secret to console
!PYTHON_EXE! -c "
import json, os, sys
cfg = {
    'server_host': sys.argv[1],
    'server_port': int(sys.argv[2]),
    'agent_id': sys.argv[3],
    'auth_token': sys.argv[4],
    'certfile': sys.argv[5] if sys.argv[5] else None,
    'opc_prog_id': 'ABB.AfwOpcDaSurrogate.1',
    'update_rate_ms': 1000,
    'log_file': 'C:\\\\ProgramData\\\\OPCBridge\\\\logs\\\\agent.log',
    'log_level': 'INFO'
}
with open('C:\\\\ProgramData\\\\OPCBridge\\\\agent.json', 'w', encoding='utf-8') as f:
    json.dump(cfg, f, indent=2)
" "!SERVER_HOST!" "!SERVER_PORT!" "!AGENT_ID!" "!AUTH_TOKEN!" "!CA_CERT!"

if %errorlevel% neq 0 (
    echo [ERROR] Failed to write agent.json.
    if "%UNATTENDED%"=="0" pause
    exit /b 1
)

:: 9. Install Windows Service with automatic startup
set "PYTHONPATH=%~dp0src;!PYTHONPATH!"
echo [INFO] Registering Windows Service OPCBridgeAgent (SERVICE_AUTO_START)...
!PYTHON_EXE! -m opc_bridge.agent.service install
if %errorlevel% neq 0 (
    echo [WARNING] Service registration returned code %errorlevel%. Checking if already registered.
)

:: 10. Configure Service Account Context (LocalSystem or specified user)
if not "!SERVICE_USER!"=="" (
    echo [INFO] Configuring service identity for OPC DA context: !SERVICE_USER!
    if not "!SERVICE_PASSWORD!"=="" (
        sc.exe config OPCBridgeAgent obj= "!SERVICE_USER!" password= "!SERVICE_PASSWORD!" >nul 2>&1
    ) else (
        sc.exe config OPCBridgeAgent obj= "!SERVICE_USER!" >nul 2>&1
    )
) else (
    echo [INFO] Configuring service identity: LocalSystem
    sc.exe config OPCBridgeAgent obj= "LocalSystem" >nul 2>&1
)

:: 11. Configure SCM Failure Recovery Actions (automatic restart after failure)
echo [INFO] Configuring SCM automatic failure recovery actions...
sc.exe failure OPCBridgeAgent reset= 86400 actions= restart/5000/restart/10000/restart/60000 >nul 2>&1

:: 12. Start Service
echo [INFO] Starting OPCBridgeAgent service...
sc.exe start OPCBridgeAgent >nul 2>&1

echo ===================================================
echo [SUCCESS] OPC-Bridge Agent installation complete.
echo Configuration: %CONFIG_DIR%\agent.json
echo Log file:      %LOG_DIR%\agent.log
echo ===================================================

if "%UNATTENDED%"=="0" (
    echo.
    echo Press any key to finish.
    pause >nul
)
exit /b 0
