@echo off
setlocal enabledelayedexpansion

:: ============================================================================
:: OPC-Bridge Windows Agent - Offline Installation & Service Setup
:: Secure, unattended-capable Windows Service installer.
:: ============================================================================

:: 1. Verify Administrator Privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Administrative privileges are required.
    echo Please right-click install.bat and select "Run as administrator".
    pause
    exit /b 1
)

:: Set working directory to script directory
pushd "%~dp0"

:: 2. Default Configuration Variables
set "SERVER_HOST=127.0.0.1"
set "SERVER_PORT=8443"
set "CA_CERT="
set "AGENT_ID=%COMPUTERNAME%-opc-01"
set "SERVICE_USER="
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
if /i "%~1"=="--unattended" (
    set "UNATTENDED=1"
    shift
    goto parse_args
)
:: Security rule: Prohibit credentials on command-line arguments (argv)
if /i "%~1"=="--token" goto reject_token_argv
if /i "%~1"=="-t" goto reject_token_argv
if /i "%~1"=="--service-password" goto reject_password_argv
if /i "%~1"=="-p" goto reject_password_argv

shift
goto parse_args

:reject_token_argv
echo [ERROR] Passing registration credentials in command line arguments is prohibited for security.
echo Please provide the token via the OPC_AUTH_TOKEN environment variable or interactive prompt.
exit /b 1

:reject_password_argv
echo [ERROR] Passing service passwords in command line arguments is prohibited for security.
echo Please provide the password via the OPC_SERVICE_PASSWORD environment variable or interactive prompt.
exit /b 1

:after_args

:: 4. Locate Python Runtime
set "PYTHON_EXE="
if exist "runtime\python.exe" (
    set "PYTHON_EXE=runtime\python.exe"
    echo [INFO] Using bundled Python runtime: !PYTHON_EXE!
    goto python_found
)
where python >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_EXE=python"
    echo [INFO] Using system Python: python
    goto python_found
)
where py >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_EXE=py -3"
    echo [INFO] Using system Python launcher: py -3
    goto python_found
)
echo [ERROR] Python 3 was not found in PATH or runtime\ folder.
echo Please install Python 3.10+ or bundle runtime\ before installing.
if "%UNATTENDED%"=="0" pause
popd
exit /b 1

:python_found

:: 5. Offline dependency installation (without network)
if not exist "wheels" goto skip_wheels
echo [INFO] Installing pinned dependencies from offline wheels directory...
!PYTHON_EXE! -m pip install --no-index --find-links="wheels" -r "requirements-offline.txt" >nul 2>&1

:skip_wheels

:: 6. Run Secure Configuration Setup Helper
set "PYTHONPATH=src;!PYTHONPATH!"
set "SETUP_ARGS=--server-host !SERVER_HOST! --server-port !SERVER_PORT! --agent-id !AGENT_ID!"
if not "!CA_CERT!"=="" set "SETUP_ARGS=!SETUP_ARGS! --ca-cert !CA_CERT!"
if not "!SERVICE_USER!"=="" set "SETUP_ARGS=!SETUP_ARGS! --service-user !SERVICE_USER!"
if "%UNATTENDED%"=="1" set "SETUP_ARGS=!SETUP_ARGS! --unattended"

echo [INFO] Executing secure configuration setup...
!PYTHON_EXE! "setup_config.py" !SETUP_ARGS!
if %errorlevel% neq 0 (
    echo [ERROR] Secure configuration setup failed. Installation aborted.
    if "%UNATTENDED%"=="0" pause
    popd
    exit /b 1
)

:: 7. Register Windows Service with automatic startup
echo [INFO] Registering Windows Service OPCBridgeAgent (SERVICE_AUTO_START)...
!PYTHON_EXE! -m opc_bridge.agent.service install
if %errorlevel% neq 0 (
    echo [WARNING] Service registration returned code %errorlevel%. Checking existing registration.
)

:: 8. Configure Service Identity securely if dedicated user specified
if not "!SERVICE_USER!"=="" (
    echo [INFO] Applying service account credentials via Win32 API...
    !PYTHON_EXE! "%~dp0setup_config.py" !SETUP_ARGS! --configure-service-user
)

:: 9. Configure SCM Failure Recovery Actions (automatic restart after failure)
echo [INFO] Configuring SCM automatic failure recovery actions...
sc.exe failure OPCBridgeAgent reset= 86400 actions= restart/5000/restart/10000/restart/60000 >nul 2>&1

:: 10. Start Service
echo [INFO] Starting OPCBridgeAgent service...
sc.exe start OPCBridgeAgent >nul 2>&1

echo ===================================================
echo [SUCCESS] OPC-Bridge Agent installation complete.
echo Configuration: C:\ProgramData\OPCBridge\agent.json
echo Log file:      C:\ProgramData\OPCBridge\logs\agent.log
echo ===================================================

if "%UNATTENDED%"=="0" (
    echo.
    echo Press any key to finish.
    pause >nul
)
popd
exit /b 0
