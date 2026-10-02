@echo off
setlocal
set "PYTHON_EXE=%~dp0runtime\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=C:\Program Files\OPCBridge\runtime\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Bundled x64 runtime is required for worker diagnostics.
    exit /b 1
)
"%PYTHON_EXE%" "%~dp0diagnostics.py"
exit /b %errorlevel%
