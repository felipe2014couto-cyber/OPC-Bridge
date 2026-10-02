@echo off
setlocal enabledelayedexpansion

:: ============================================================================
:: OPC-Bridge Windows Agent - Uninstallation
:: Stops the Windows Service, deletes its registration from SCM,
:: removes installed binaries from C:\Program Files\OPCBridge\,
:: and generates a persistent uninstall.log in C:\ProgramData\OPCBridge\logs\.
:: The logs directory is strictly preserved.
:: ============================================================================

set "SCRIPT_DIR=%~dp0"
set "LOG_DIR=C:\ProgramData\OPCBridge\logs"
if not exist "!LOG_DIR!" mkdir "!LOG_DIR!" >nul 2>&1
set "UNINSTALL_LOG=!LOG_DIR!\uninstall.log"

echo [%date% %time%] [INFO] [uninstaller] =================================================== >> "!UNINSTALL_LOG!"
echo [%date% %time%] [INFO] [uninstaller] Starting OPC-Bridge Agent Uninstallation >> "!UNINSTALL_LOG!"
echo [%date% %time%] [INFO] [uninstaller] Host: %COMPUTERNAME%, Arch: %PROCESSOR_ARCHITECTURE% >> "!UNINSTALL_LOG!"
echo [%date% %time%] [INFO] [uninstaller] Invocation Directory: !SCRIPT_DIR! >> "!UNINSTALL_LOG!"

echo ===================================================
echo   OPC-Bridge Windows Agent - Uninstallation
echo ===================================================

:: Check for Administrator privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] This uninstaller requires Administrator privileges.
    echo Please right-click uninstall.bat and select "Run as administrator".
    echo [%date% %time%] [ERROR] [uninstaller] Administrative privileges missing. Aborting. >> "!UNINSTALL_LOG!"
    pause
    exit /b 1
)

set "UNATTENDED=0"
:parse_args
if "%~1"=="" goto after_args
if /i "%~1"=="--unattended" set "UNATTENDED=1"
if /i "%~1"=="/unattended" set "UNATTENDED=1"
if /i "%~1"=="-unattended" set "UNATTENDED=1"
shift /1
goto parse_args
:after_args

set "INSTALL_DIR=%ProgramFiles%\OPCBridge"
if not defined ProgramFiles set "INSTALL_DIR=C:\Program Files\OPCBridge"

set "IS_RUNNING_FROM_INSTALL=0"
if /i "!SCRIPT_DIR!"=="!INSTALL_DIR!\" set "IS_RUNNING_FROM_INSTALL=1"
echo [%date% %time%] [INFO] [uninstaller] Context: IS_RUNNING_FROM_INSTALL=!IS_RUNNING_FROM_INSTALL!, SCRIPT_DIR=[!SCRIPT_DIR!], target=[!INSTALL_DIR!\] >> "!UNINSTALL_LOG!"

:: Locate Python executable (installed or local)
set "PYTHON_EXE="
if exist "!INSTALL_DIR!\runtime\python.exe" set "PYTHON_EXE=!INSTALL_DIR!\runtime\python.exe"
if not defined PYTHON_EXE if exist "!SCRIPT_DIR!runtime\python.exe" set "PYTHON_EXE=!SCRIPT_DIR!runtime\python.exe"
if not defined PYTHON_EXE if exist "runtime\python.exe" set "PYTHON_EXE=runtime\python.exe"

echo [INFO] Stopping OPCBridgeAgent service...
echo [%date% %time%] [INFO] [uninstaller] Stopping service OPCBridgeAgent... >> "!UNINSTALL_LOG!"
sc.exe stop OPCBridgeAgent >> "!UNINSTALL_LOG!" 2>&1

:: Poll SCM until stopped or timeout (up to 10 seconds)
for /L %%i in (1,1,10) do (
    sc.exe query OPCBridgeAgent 2>&1 | findstr /i "STOPPED" >nul
    if !errorlevel! equ 0 goto svc_stopped
    sc.exe query OPCBridgeAgent 2>&1 | findstr /i "1060" >nul
    if !errorlevel! equ 0 goto svc_stopped
    ping -n 2 127.0.0.1 >nul 2>&1
)
:svc_stopped

if defined PYTHON_EXE (
    echo [INFO] Removing Windows Service registration via service manager...
    echo [%date% %time%] [INFO] [uninstaller] Removing service via opc_bridge.agent.service remove >> "!UNINSTALL_LOG!"
    set "PYTHONPATH=!INSTALL_DIR!\src;!PYTHONPATH!"
    "!PYTHON_EXE!" -m opc_bridge.agent.service remove >> "!UNINSTALL_LOG!" 2>&1
)

:: Ensure service is deleted from SCM
sc.exe query OPCBridgeAgent >nul 2>&1
if %errorlevel% equ 0 (
    echo [INFO] Deleting Windows Service via sc.exe delete...
    echo [%date% %time%] [INFO] [uninstaller] Deleting service via sc.exe delete >> "!UNINSTALL_LOG!"
    sc.exe delete OPCBridgeAgent >> "!UNINSTALL_LOG!" 2>&1
)

sc.exe query OPCBridgeAgent >nul 2>&1
if %errorlevel% neq 0 (
    echo [%date% %time%] [INFO] [uninstaller] Windows Service successfully removed from SCM. >> "!UNINSTALL_LOG!"
) else (
    echo [%date% %time%] [WARNING] [uninstaller] Service is marked for deletion and will terminate when handles close. >> "!UNINSTALL_LOG!"
)

:: Remove installed binaries in C:\Program Files\OPCBridge
if exist "!INSTALL_DIR!" (
    echo [INFO] Removing installed binaries from !INSTALL_DIR!...
    echo [%date% %time%] [INFO] [uninstaller] Removing installed binaries from !INSTALL_DIR!... >> "!UNINSTALL_LOG!"

    :: Remove subdirectories
    if exist "!INSTALL_DIR!\runtime-x86" rmdir /s /q "!INSTALL_DIR!\runtime-x86" >> "!UNINSTALL_LOG!" 2>&1
    if exist "!INSTALL_DIR!\runtime" rmdir /s /q "!INSTALL_DIR!\runtime" >> "!UNINSTALL_LOG!" 2>&1
    if exist "!INSTALL_DIR!\src" rmdir /s /q "!INSTALL_DIR!\src" >> "!UNINSTALL_LOG!" 2>&1
    if exist "!INSTALL_DIR!\wheels" rmdir /s /q "!INSTALL_DIR!\wheels" >> "!UNINSTALL_LOG!" 2>&1
    if exist "!INSTALL_DIR!\config" rmdir /s /q "!INSTALL_DIR!\config" >> "!UNINSTALL_LOG!" 2>&1
    if exist "!INSTALL_DIR!\tools" rmdir /s /q "!INSTALL_DIR!\tools" >> "!UNINSTALL_LOG!" 2>&1
    echo [%date% %time%] [INFO] [uninstaller] Removed subdirectories. Cleaning files... >> "!UNINSTALL_LOG!"

    :: Remove files
    if "!IS_RUNNING_FROM_INSTALL!"=="1" (
        echo [%date% %time%] [INFO] [uninstaller] Running inside install dir: selective file deletion >> "!UNINSTALL_LOG!"
        del /f /q "!INSTALL_DIR!\*.py" >> "!UNINSTALL_LOG!" 2>&1
        del /f /q "!INSTALL_DIR!\*.txt" >> "!UNINSTALL_LOG!" 2>&1
        del /f /q "!INSTALL_DIR!\*.md" >> "!UNINSTALL_LOG!" 2>&1
        del /f /q "!INSTALL_DIR!\*.json" >> "!UNINSTALL_LOG!" 2>&1
        del /f /q "!INSTALL_DIR!\*.whl" >> "!UNINSTALL_LOG!" 2>&1
        del /f /q "!INSTALL_DIR!\install.bat" >> "!UNINSTALL_LOG!" 2>&1
        del /f /q "!INSTALL_DIR!\diagnostics.bat" >> "!UNINSTALL_LOG!" 2>&1
        del /f /q "!INSTALL_DIR!\run_foreground.bat" >> "!UNINSTALL_LOG!" 2>&1
    ) else (
        echo [%date% %time%] [INFO] [uninstaller] Running outside install dir: full removal >> "!UNINSTALL_LOG!"
        del /f /q "!INSTALL_DIR!\*.*" >> "!UNINSTALL_LOG!" 2>&1
        cd /d "%TEMP%"
        rmdir /s /q "!INSTALL_DIR!" >> "!UNINSTALL_LOG!" 2>&1
    )
)

echo [%date% %time%] [INFO] [uninstaller] Preserving logs in C:\ProgramData\OPCBridge\logs\ >> "!UNINSTALL_LOG!"
echo [%date% %time%] [INFO] [uninstaller] Uninstallation completed successfully. >> "!UNINSTALL_LOG!"

echo ===================================================
echo [SUCCESS] OPC-Bridge Agent service and binaries removed.
echo Note: Logs and configuration in C:\ProgramData\OPCBridge\
echo were preserved.
echo Uninstall log: C:\ProgramData\OPCBridge\logs\uninstall.log
echo ===================================================

if "%UNATTENDED%"=="0" pause

if "!IS_RUNNING_FROM_INSTALL!"=="1" (
    cd /d "%TEMP%"
    wmic process call create "cmd.exe /c ping -n 2 127.0.0.1 >nul & rmdir /s /q \"!INSTALL_DIR!\"" >nul 2>&1
    if !errorlevel! neq 0 (
        powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Process cmd.exe -ArgumentList '/c ping -n 2 127.0.0.1 >nul & rmdir /s /q \"\"!INSTALL_DIR!\"\"' -WindowStyle Hidden" >nul 2>&1
    )
)
exit /b 0
