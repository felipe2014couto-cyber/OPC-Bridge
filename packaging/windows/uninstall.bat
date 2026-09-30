@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo   OPC-Bridge Windows Agent - Uninstallation
echo ===================================================

:: Check for Administrator privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] This uninstaller requires Administrator privileges.
    echo Please right-click uninstall.bat and select "Run as administrator".
    pause
    exit /b 1
)

pushd "%~dp0"

:: Locate Python executable
set "PYTHON_EXE="
if exist "runtime\python.exe" (
    set "PYTHON_EXE=runtime\python.exe"
    goto python_found
)
where python >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_EXE=python"
    goto python_found
)
where py >nul 2>&1
if %errorlevel% equ 0 (
    set "PYTHON_EXE=py -3"
    goto python_found
)

:python_found

echo [INFO] Stopping OPCBridgeAgent service...
sc.exe stop OPCBridgeAgent >nul 2>&1
ping -n 3 127.0.0.1 >nul 2>&1

if defined PYTHON_EXE (
    echo [INFO] Removing Windows Service registration...
    set "PYTHONPATH=src;!PYTHONPATH!"
    !PYTHON_EXE! -m opc_bridge.agent.service remove
) else (
    echo [INFO] Removing Windows Service via sc.exe delete...
    sc.exe delete OPCBridgeAgent >nul 2>&1
)

echo ===================================================
echo [SUCCESS] OPC-Bridge Agent service removed.
echo Note: Logs and configuration in C:\ProgramData\OPCBridge
echo were preserved. Delete them manually if no longer needed.
echo ===================================================
popd
if not "%~1"=="--unattended" if not "%UNATTENDED%"=="1" pause
exit /b 0
