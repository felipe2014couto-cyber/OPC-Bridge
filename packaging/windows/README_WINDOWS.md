# OPC-Bridge Agent — Windows Deployment & Operation Guide

## Overview
This package contains the offline distribution of the **OPC-Bridge Agent** for Windows hosts running OPC DA servers (such as `ABB.AfwOpcDaSurrogate.1` or standard OPC DA 2.0 servers).

It includes:
- **Supervised COM Isolation**: Runs OPC COM communication in a separate worker process to protect the Windows service from COM threading violations, DLL memory crashes, or driver deadlocks.
- **Windows Service**: Configured with automatic startup (`SERVICE_AUTO_START`) and SCM recovery actions (automatic restart on failure).
- **Offline Installation**: Fully functional without internet access or development tools on the target machine.

---

## Directory Layout
```text
opc-bridge-agent-windows-offline/
├── runtime/                # x64 main service and x64 OPC worker
├── runtime-x86/            # Python 3.8.10 isolated x86 OPC worker
├── manifest.json           # Both runtimes and complete file inventory
├── src/
│   └── opc_bridge/          # Python agent implementation
├── config/
│   └── agent.default.json   # Default configuration template
├── wheels/                  # Pinned offline Python wheels (e.g. pywin32)
├── requirements-offline.txt # Pinned dependencies
├── install.bat              # Automated service installer (Run as Admin)
├── uninstall.bat            # Service removal script (Run as Admin)
├── run_foreground.bat       # Interactive diagnostic console runner
└── README_WINDOWS.md        # This guide
```

---

## Prerequisites on Industrial Host
1. **Windows OS**: Windows x64 capable of running both bundled runtimes.
2. **Python**: Both prepared runtimes are bundled; host Python is not used.
3. **OPC DA Server**: Registered OPC DA server on the machine (e.g., ABB System 800xA with `ABB.AfwOpcDaSurrogate.1`).
4. **No development tools required**: No compiler, git, or internet connection needed.

---

## Installation Steps (Offline)
1. Extract the release ZIP to a temporary staging directory. The installer copies it
   to `C:\Program Files\OPCBridge`; keep staging separate from that destination.
2. Right-click `install.bat` and select **"Run as administrator"**.
   The installer will:
   - Validate both prepared runtimes and let you choose the OPC worker architecture.
   - Write `C:\ProgramData\OPCBridge\agent.json` using securely supplied credentials.
   - Register the Windows Service `OPCBridgeAgent` with automatic startup (`SERVICE_AUTO_START`).
   - Configure SCM Failure Actions to automatically restart the service after any crash.
   - Start the service.

---

## Configuration
Edit `C:\ProgramData\OPCBridge\agent.json`:
```json
{
  "server_host": "10.0.0.10",
  "server_port": 8443,
  "agent_id": "abb-node-01",
  "auth_token": "your_secure_pre_shared_token",
  "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
  "worker_architecture": "auto",
  "update_rate_ms": 1000,
  "log_file": "C:\\ProgramData\\OPCBridge\\logs\\agent.log",
  "log_level": "INFO"
}
```
After editing, restart the service:
```cmd
net stop OPCBridgeAgent
net start OPCBridgeAgent
```

---

## Diagnostic & Foreground Testing
To verify OPC DA connectivity and server communication interactively before running as a service:
1. Double-click `run_foreground.bat`.
2. Observe console logs, OPC connection status, and read durations.
3. Press `Ctrl+C` to terminate.

---

## Service Management Commands
- **Check Status**: `sc.exe query OPCBridgeAgent`
- **Start**: `net start OPCBridgeAgent` or `sc.exe start OPCBridgeAgent`
- **Stop**: `net stop OPCBridgeAgent` or `sc.exe stop OPCBridgeAgent`
- **Uninstall**: Run `uninstall.bat` as administrator.

## Dual-runtime OPC workers

The Windows service always uses the bundled **x64** runtime in `runtime/`.
Only the isolated OPC DA worker can use x86. The additional runtime is placed
at `runtime-x86/python.exe`; host Python and simulated COM are never fallbacks.

`agent.json` accepts exactly `"worker_architecture": "auto"`, `"x64"` or `"x86"`.
The default is `auto`: inspect the x64 registry view first, then x86, requiring
`OPC.Automation/CLSID` and a non-empty `InprocServer32` or `LocalServer32` value.
An explicit architecture checks only its own view and fails if unavailable.
A missing/wrong bundled runtime is an error, even if another runtime is present.
The selected worker also validates registration in its own process before COM
activation. Registry inspection does not prove that the DLL loads or OPC works.
No DCOM/COM registration is changed by selection.

Interactive `install.bat` prompts for `auto`, `x64` or `x86` (default `auto`).
For unattended installation, supply the choice explicitly:

```bat
install.bat --unattended --worker-architecture auto
install.bat --unattended --worker-architecture x86
```

Existing central-address, CA and secure credential options still apply.
The configured choice is stored in ProgramData `agent.json`; the requested and
selected architectures and registration reason appear in `install.log` and
`agent.log`. `diagnostics.bat` reports the choice and both runtime versions,
without activating COM or dumping credentials. Service-account permissions
and COM authorization remain prerequisites; changing bitness does not grant access.

Prepare both relocatable runtimes on the build machine **before** packaging:

- x64: the current main CPython runtime, with pywin32 **312** already installed,
  a matching `pywin32-312-cpXY-cpXY-win_amd64.whl`, and x64 `pythonservice.exe`.
  The actual CPython version and wheel filename are recorded in the manifest.
- x86: CPython **3.8.10 Windows x86** with pywin32 **306**, using exactly
  `pywin32-306-cp38-cp38-win32.whl`. Python 3.8 is a legacy, end-of-life runtime;
  this pin provides the project's Python 3.8 compatibility, not current security support.
  Sources: [Python 3.8.10](https://www.python.org/downloads/release/python-3810/)
  and [pywin32 306](https://pypi.org/project/pywin32/306/).

The builder requires explicit local inputs, copies prepared dependencies and
application source into **both** runtimes, and performs no pip/download step:

```bat
python packaging\windows\build_package.py --runtime-dir C:\build-inputs\runtime-x64 --runtime-x86-dir C:\build-inputs\runtime-x86 --wheels-dir C:\build-inputs\wheels --output-dir dist
```

Use a new output directory; existing artifacts are never overwritten.
The build command is for the build workstation only. Installation invokes
bundled interpreters exclusively and needs no internet or host Python.
`manifest.json` includes `worker_runtimes.x64` and `.x86`, version pins,
relative runtime paths, exact wheel names, full file hashes and the build commit.
`verify_package.py` accepts an extracted folder or ZIP and requires both
runtimes, installed pywin32 COM DLLs with matching PE architecture, wheels,
worker sources and the x64 service executable. Verification neither activates
COM nor reads OPC values.

```bat
runtime\python.exe verify_package.py .
runtime\python.exe verify_package.py C:\build-artifacts\opc-bridge-agent-windows-offline.zip
```

Linux tests use mocked registry and in-memory package inventories. Windows
rebuild, actual x64/x86 subprocess IPC, DLL loading, installer execution and
real OPC reads require separate authorized validation. Do not treat these
simulated tests as ABB/COM validation.
