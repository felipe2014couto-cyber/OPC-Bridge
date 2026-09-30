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
1. **Windows OS**: Windows 10/11, Windows Server 2016/2019/2022 (x64 or x86).
2. **Python**: Python 3.10+ installed or provided via bundled `runtime\` folder.
3. **OPC DA Server**: Registered OPC DA server on the machine (e.g., ABB System 800xA with `ABB.AfwOpcDaSurrogate.1`).
4. **No development tools required**: No compiler, git, or internet connection needed.

---

## Installation Steps (Offline)
1. Extract the release ZIP (`opc-bridge-agent-windows-offline.zip`) to a permanent location, e.g.:
   `C:\Program Files\OPCBridge` or `C:\OPCBridge`
2. Right-click `install.bat` and select **"Run as administrator"**.
   The installer will:
   - Install dependencies offline from `wheels\`.
   - Create `C:\ProgramData\OPCBridge\agent.json` (if not already existing).
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
