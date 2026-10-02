@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo   OPC-Bridge Windows Agent - Interactive Console
echo ===================================================

:: Locate Python executable
set "PYTHON_EXE="
if exist "%~dp0runtime\python.exe" (
    set "PYTHON_EXE=%~dp0runtime\python.exe"
) else (
    echo [ERROR] Bundled x64 runtime is missing.
    exit /b 1
)

set "PYTHONPATH=%~dp0src;!PYTHONPATH!"
set "CONFIG_PATH=%~dp0config\agent.default.json"
if exist "C:\ProgramData\OPCBridge\agent.json" (
    set "CONFIG_PATH=C:\ProgramData\OPCBridge\agent.json"
)

echo [INFO] Running in foreground with configuration: !CONFIG_PATH!
echo Press Ctrl+C to stop.
echo.

"!PYTHON_EXE!" -m opc_bridge.agent.service --run --config "!CONFIG_PATH!"

pause
