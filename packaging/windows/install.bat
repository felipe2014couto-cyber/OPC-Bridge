@echo off
setlocal enabledelayedexpansion

:: ============================================================================
:: OPC-Bridge Windows Agent - Offline Installation & Service Setup
:: Fully self-contained, unattended-capable Windows Service installer.
:: Installs binaries to C:\Program Files\OPCBridge\ (durable location).
:: Stores configuration, CA, and logs in C:\ProgramData\OPCBridge\.
:: Host Python fallback is strictly prohibited.
:: ============================================================================

:: 1. Ensure log directory and define persistent install.log
set "LOG_DIR=C:\ProgramData\OPCBridge\logs"
if not exist "!LOG_DIR!" mkdir "!LOG_DIR!" >nul 2>&1
set "INSTALL_LOG=!LOG_DIR!\install.log"

echo [%date% %time%] [INFO] [installer] =================================================== >> "!INSTALL_LOG!"
echo [%date% %time%] [INFO] [installer] Starting OPC-Bridge Agent Installation >> "!INSTALL_LOG!"
echo [%date% %time%] [INFO] [installer] Host: %COMPUTERNAME%, Arch: %PROCESSOR_ARCHITECTURE% >> "!INSTALL_LOG!"
echo [%date% %time%] [INFO] [installer] Extraction/Source Directory: %~dp0 >> "!INSTALL_LOG!"

:: 2. Verify Administrator Privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Administrative privileges are required.
    echo Please right-click install.bat and select "Run as administrator".
    echo [%date% %time%] [ERROR] [installer] Administrative privileges missing. Aborting. >> "!INSTALL_LOG!"
    echo [%date% %time%] [DIAGNOSTIC] [installer] Right-click install.bat and select "Run as administrator". >> "!INSTALL_LOG!"
    pause
    exit /b 1
)

:: Set working directory to extraction directory
pushd "%~dp0"

:: 3. Default Configuration Variables
set "SERVER_HOST="
set "SERVER_PORT="
set "SERVER_HOSTNAME="
set "CA_CERT="
set "AGENT_ID="
set "SERVICE_USER="
set "UNATTENDED=0"
set "WORKER_ARCHITECTURE="

:: 4. Parse Command-Line Arguments
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
if /i "%~1"=="--server-hostname" (
    set "SERVER_HOSTNAME=%~2"
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
if /i "%~1"=="--worker-architecture" (
    if "%~2"=="" goto invalid_worker_architecture
    set "WORKER_ARCHITECTURE=%~2"
    shift & shift
    goto parse_args
)
if /i "%~1"=="--unattended" (
    set "UNATTENDED=1"
    shift
    goto parse_args
)
if /i "%~1"=="/unattended" (
    set "UNATTENDED=1"
    shift
    goto parse_args
)
if /i "%~1"=="-unattended" (
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
echo Please provide the token via the OPC_AUTH_TOKEN environment variable, bootstrap config, or interactive prompt.
echo [%date% %time%] [ERROR] [installer] Credentials passed on command-line rejected for security. >> "!INSTALL_LOG!"
echo [%date% %time%] [DIAGNOSTIC] [installer] Use OPC_AUTH_TOKEN environment variable or config\agent.bootstrap.json. >> "!INSTALL_LOG!"
exit /b 1

:reject_password_argv
echo [ERROR] Passing service passwords in command line arguments is prohibited for security.
echo Please provide the password via the OPC_SERVICE_PASSWORD environment variable or interactive prompt.
echo [%date% %time%] [ERROR] [installer] Service password on command-line rejected for security. >> "!INSTALL_LOG!"
echo [%date% %time%] [DIAGNOSTIC] [installer] Use OPC_SERVICE_PASSWORD environment variable or interactive prompt. >> "!INSTALL_LOG!"
exit /b 1

:invalid_worker_architecture
echo [ERROR] --worker-architecture must be auto, x64 or x86.
popd
exit /b 1

:after_args

if not defined WORKER_ARCHITECTURE (
    if "!UNATTENDED!"=="1" goto invalid_worker_architecture
    echo OPC worker only: auto prefers registered x64, then x86. Main service stays x64.
    set /p "WORKER_ARCHITECTURE=Choose auto, x64 or x86 [auto]: "
    if not defined WORKER_ARCHITECTURE set "WORKER_ARCHITECTURE=auto"
)
if not "!WORKER_ARCHITECTURE!"=="auto" if not "!WORKER_ARCHITECTURE!"=="x64" if not "!WORKER_ARCHITECTURE!"=="x86" goto invalid_worker_architecture
echo [%date% %time%] [INFO] [installer] Worker architecture requested: !WORKER_ARCHITECTURE! >> "!INSTALL_LOG!"

:: Log parsed arguments safely (excluding secrets)
echo [%date% %time%] [INFO] [installer] Parsed options: server=!SERVER_HOST!:!SERVER_PORT!, hostname=!SERVER_HOSTNAME!, agent_id=!AGENT_ID!, ca=!CA_CERT!, unattended=!UNATTENDED! >> "!INSTALL_LOG!"

:: 5. Locate Bundled Python Runtime in Package
set "PKG_PYTHON="
if exist "runtime\python.exe" (
    set "PKG_PYTHON=runtime\python.exe"
    echo [INFO] Located bundled Python runtime in extraction folder.
    echo [%date% %time%] [INFO] [installer] Located bundled Python runtime: %~dp0runtime\python.exe >> "!INSTALL_LOG!"
    goto pkg_python_found
)
echo [ERROR] Bundled Python runtime (runtime\python.exe) missing.
echo Offline installation requires the internal Python runtime included in this package.
echo Installation will not fall back to host Python. Aborting.
echo [%date% %time%] [ERROR] [installer] Bundled runtime (runtime\python.exe) missing from %~dp0 >> "!INSTALL_LOG!"
echo [%date% %time%] [DIAGNOSTIC] [installer] Re-extract the full package ZIP ensuring the runtime\ subfolder is intact. >> "!INSTALL_LOG!"
if "%UNATTENDED%"=="0" pause
popd
exit /b 1

:pkg_python_found
if not exist "runtime-x86\python.exe" goto invalid_worker_architecture

:: 6. Verify Package Integrity in Extraction Folder
if not exist "verify_package.py" goto invalid_worker_architecture
if exist "verify_package.py" (
    echo [INFO] Verifying package integrity...
    echo [%date% %time%] [INFO] [installer] Running verify_package.py on package directory >> "!INSTALL_LOG!"
    "!PKG_PYTHON!" "verify_package.py" . >> "!INSTALL_LOG!" 2>&1
    if !errorlevel! neq 0 (
        echo [ERROR] Package verification failed. Package files may be corrupted or tampered.
        echo [%date% %time%] [ERROR] [installer] verify_package.py reported error code !errorlevel!. >> "!INSTALL_LOG!"
        echo [%date% %time%] [DIAGNOSTIC] [installer] Inspect !INSTALL_LOG! above for missing required files or hash mismatches. >> "!INSTALL_LOG!"
        if "%UNATTENDED%"=="0" pause
        popd
        exit /b 1
    )
    echo [%date% %time%] [INFO] [installer] Package integrity check passed. >> "!INSTALL_LOG!"
)

:: Validate worker choice before stopping an existing service or copying binaries.
"!PKG_PYTHON!" -I -B -c "import sys,logging; logging.basicConfig(level=logging.INFO); from opc_bridge.adapters.worker_runtime import select_worker_runtime; select_worker_runtime(sys.argv[1], sys.executable)" "!WORKER_ARCHITECTURE!" >> "!INSTALL_LOG!" 2>&1
if !errorlevel! neq 0 goto invalid_worker_architecture

:: 7. Copy Agent Files to Durable Location (C:\Program Files\OPCBridge)
set "INSTALL_DIR=%ProgramFiles%\OPCBridge"
if not defined ProgramFiles set "INSTALL_DIR=C:\Program Files\OPCBridge"

echo [INFO] Installing agent files to durable location: !INSTALL_DIR!
echo [%date% %time%] [INFO] [installer] Installing agent files to durable location: !INSTALL_DIR! >> "!INSTALL_LOG!"

if not exist "!INSTALL_DIR!" mkdir "!INSTALL_DIR!" >nul 2>&1

:: Stop running service before overwriting binaries if already installed
sc.exe query OPCBridgeAgent >nul 2>&1
if %errorlevel% equ 0 (
    echo [INFO] Stopping existing OPCBridgeAgent service before binary update...
    sc.exe stop OPCBridgeAgent >> "!INSTALL_LOG!" 2>&1
    ping -n 3 127.0.0.1 >nul 2>&1
)

:: Copy runtime
echo [INFO] Copying internal Python runtime...
xcopy "%~dp0runtime" "!INSTALL_DIR!\runtime\" /E /I /Y /Q >> "!INSTALL_LOG!" 2>&1

xcopy "%~dp0runtime-x86" "!INSTALL_DIR!\runtime-x86\" /E /I /Y /Q >> "!INSTALL_LOG!" 2>&1
if !errorlevel! geq 1 goto invalid_worker_architecture

:: Copy source code
echo [INFO] Copying agent application code...
xcopy "%~dp0src" "!INSTALL_DIR!\src\" /E /I /Y /Q >> "!INSTALL_LOG!" 2>&1

:: Copy offline wheels if present
if exist "%~dp0wheels" (
    echo [INFO] Copying offline wheels...
    xcopy "%~dp0wheels" "!INSTALL_DIR!\wheels\" /E /I /Y /Q >> "!INSTALL_LOG!" 2>&1
)

:: Copy packaging config if present
if exist "%~dp0config" (
    xcopy "%~dp0config" "!INSTALL_DIR!\config\" /E /I /Y /Q >> "!INSTALL_LOG!" 2>&1
)

:: Copy tools, scripts and manifest to installed directory
copy /y "%~dp0requirements-offline.txt" "!INSTALL_DIR!\" >> "!INSTALL_LOG!" 2>&1
copy /y "%~dp0setup_config.py" "!INSTALL_DIR!\" >> "!INSTALL_LOG!" 2>&1
copy /y "%~dp0verify_package.py" "!INSTALL_DIR!\" >> "!INSTALL_LOG!" 2>&1
copy /y "%~dp0diagnostics.bat" "!INSTALL_DIR!\" >> "!INSTALL_LOG!" 2>&1
copy /y "%~dp0diagnostics.py" "!INSTALL_DIR!\" >> "!INSTALL_LOG!" 2>&1
copy /y "%~dp0uninstall.bat" "!INSTALL_DIR!\" >> "!INSTALL_LOG!" 2>&1
if exist "%~dp0run_foreground.bat" copy /y "%~dp0run_foreground.bat" "!INSTALL_DIR!\" >> "!INSTALL_LOG!" 2>&1
if exist "%~dp0manifest.json" copy /y "%~dp0manifest.json" "!INSTALL_DIR!\" >> "!INSTALL_LOG!" 2>&1
if exist "%~dp0README_WINDOWS.md" copy /y "%~dp0README_WINDOWS.md" "!INSTALL_DIR!\" >> "!INSTALL_LOG!" 2>&1

:: Verify installed runtime in durable location
set "PYTHON_EXE=!INSTALL_DIR!\runtime\python.exe"
if not exist "!PYTHON_EXE!" (
    echo [ERROR] Failed to install Python runtime to !INSTALL_DIR!\runtime\python.exe
    echo [%date% %time%] [ERROR] [installer] Installed runtime missing at !PYTHON_EXE! >> "!INSTALL_LOG!"
    if "%UNATTENDED%"=="0" pause
    popd
    exit /b 1
)
echo [%date% %time%] [INFO] [installer] Durable installation verified at !INSTALL_DIR! >> "!INSTALL_LOG!"

:: Leave extraction folder and execute setup from installed location
popd
pushd "!INSTALL_DIR!"

:: Both runtimes arrive with pinned pywin32 installed; no pip or host Python is used.
"!PYTHON_EXE!" -I -B -c "import struct; from importlib.metadata import version; assert struct.calcsize('P') == 8; assert version('pywin32') == '312'" >> "!INSTALL_LOG!" 2>&1
if !errorlevel! neq 0 goto invalid_worker_architecture
"!INSTALL_DIR!\runtime-x86\python.exe" -I -B -c "import sys,struct; from importlib.metadata import version; assert struct.calcsize('P') == 4; assert sys.version_info[:3] == (3,8,10); assert version('pywin32') == '306'" >> "!INSTALL_LOG!" 2>&1
if !errorlevel! neq 0 goto invalid_worker_architecture

:: 9. Run Secure Configuration Setup Helper
set "PYTHONPATH=!INSTALL_DIR!\src;!PYTHONPATH!"
set "SETUP_ARGS=--worker-architecture !WORKER_ARCHITECTURE!"
if not "!SERVER_HOST!"=="" set "SETUP_ARGS=!SETUP_ARGS! --server-host !SERVER_HOST!"
if not "!SERVER_PORT!"=="" set "SETUP_ARGS=!SETUP_ARGS! --server-port !SERVER_PORT!"
if not "!SERVER_HOSTNAME!"=="" set "SETUP_ARGS=!SETUP_ARGS! --server-hostname !SERVER_HOSTNAME!"
if not "!AGENT_ID!"=="" set "SETUP_ARGS=!SETUP_ARGS! --agent-id !AGENT_ID!"
if "!CA_CERT!"=="" if exist "config\ca.pem" set "CA_CERT=config\ca.pem"
if not "!CA_CERT!"=="" set "SETUP_ARGS=!SETUP_ARGS! --ca-cert !CA_CERT!"
if exist "config\agent.bootstrap.json" set "SETUP_ARGS=!SETUP_ARGS! --bootstrap-file config\agent.bootstrap.json"
if not "!SERVICE_USER!"=="" set "SETUP_ARGS=!SETUP_ARGS! --service-user !SERVICE_USER!"
if "%UNATTENDED%"=="1" set "SETUP_ARGS=!SETUP_ARGS! --unattended"

echo [INFO] Executing secure configuration setup...
echo [%date% %time%] [INFO] [installer] Running setup_config.py from !INSTALL_DIR! >> "!INSTALL_LOG!"
"!PYTHON_EXE!" "setup_config.py" !SETUP_ARGS! >> "!INSTALL_LOG!" 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Secure configuration setup failed. Installation aborted.
    echo [%date% %time%] [ERROR] [installer] setup_config.py failed with exit code %errorlevel%. >> "!INSTALL_LOG!"
    echo [%date% %time%] [DIAGNOSTIC] [installer] Check if auth_token was provided via OPC_AUTH_TOKEN or config\agent.bootstrap.json. >> "!INSTALL_LOG!"
    if "%UNATTENDED%"=="0" pause
    popd
    exit /b 1
)
echo [%date% %time%] [INFO] [installer] Configuration and CA certificate installed to C:\ProgramData\OPCBridge. >> "!INSTALL_LOG!"

:: 10. Register Windows Service with durable binary path
echo [INFO] Registering Windows Service OPCBridgeAgent from durable location...
echo [%date% %time%] [INFO] [installer] Registering Windows Service OPCBridgeAgent via !PYTHON_EXE!... >> "!INSTALL_LOG!"

:: Remove existing registration if present to ensure clean durable path
sc.exe query OPCBridgeAgent >nul 2>&1
if %errorlevel% equ 0 (
    "!PYTHON_EXE!" -m opc_bridge.agent.service remove >> "!INSTALL_LOG!" 2>&1
    sc.exe delete OPCBridgeAgent >> "!INSTALL_LOG!" 2>&1
    ping -n 2 127.0.0.1 >nul 2>&1
)

:: Install service using the durable Python runtime
"!PYTHON_EXE!" -m opc_bridge.agent.service install >> "!INSTALL_LOG!" 2>&1
if %errorlevel% neq 0 (
    echo [WARNING] Service registration returned code %errorlevel%. Checking SCM.
    echo [%date% %time%] [WARNING] [installer] Service install returned %errorlevel%, checking SCM. >> "!INSTALL_LOG!"
)

sc.exe query OPCBridgeAgent >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Windows Service OPCBridgeAgent registration failed in SCM.
    echo [%date% %time%] [ERROR] [installer] Service not found in SCM after registration attempt. >> "!INSTALL_LOG!"
    echo [%date% %time%] [DIAGNOSTIC] [installer] Ensure pythonservice.exe is present in !INSTALL_DIR!\runtime and check Event Log. >> "!INSTALL_LOG!"
    if "%UNATTENDED%"=="0" pause
    popd
    exit /b 1
)

:: Verify and log the registered binary path
echo [%date% %time%] [INFO] [installer] Querying service configuration in SCM: >> "!INSTALL_LOG!"
sc.exe qc OPCBridgeAgent >> "!INSTALL_LOG!" 2>&1
echo [%date% %time%] [INFO] [installer] Windows Service registered successfully in SCM. >> "!INSTALL_LOG!"

:: 11. Configure Service Identity securely if dedicated user specified
if not "!SERVICE_USER!"=="" (
    echo [INFO] Applying service account credentials via Win32 API...
    echo [%date% %time%] [INFO] [installer] Applying service logon user: !SERVICE_USER! >> "!INSTALL_LOG!"
    "!PYTHON_EXE!" "setup_config.py" !SETUP_ARGS! --configure-service-user >> "!INSTALL_LOG!" 2>&1
)

:: 12. Configure SCM Failure Recovery Actions (automatic restart after failure)
echo [INFO] Configuring SCM automatic failure recovery actions...
echo [%date% %time%] [INFO] [installer] Configuring SCM failure recovery (restart 5s/10s/60s)... >> "!INSTALL_LOG!"
sc.exe failure OPCBridgeAgent reset= 86400 actions= restart/5000/restart/10000/restart/60000 >> "!INSTALL_LOG!" 2>&1

:: 13. Start Service
echo [INFO] Starting OPCBridgeAgent service...
echo [%date% %time%] [INFO] [installer] Starting service OPCBridgeAgent... >> "!INSTALL_LOG!"
sc.exe start OPCBridgeAgent >> "!INSTALL_LOG!" 2>&1
ping -n 3 127.0.0.1 >nul 2>&1
sc.exe query OPCBridgeAgent >> "!INSTALL_LOG!" 2>&1

echo [%date% %time%] [INFO] [installer] Installation completed successfully. >> "!INSTALL_LOG!"
echo ===================================================
echo [SUCCESS] OPC-Bridge Agent installation complete.
echo Installed at:   !INSTALL_DIR!
echo Configuration:  C:\ProgramData\OPCBridge\agent.json
echo Log file:       C:\ProgramData\OPCBridge\logs\agent.log
echo Install log:    C:\ProgramData\OPCBridge\logs\install.log
echo Diagnostics:    !INSTALL_DIR!\diagnostics.bat
echo Note: Temporary extraction folder may now be safely deleted.
echo ===================================================

if "%UNATTENDED%"=="0" (
    echo.
    echo Press any key to finish.
    pause >nul
)
popd
exit /b 0
