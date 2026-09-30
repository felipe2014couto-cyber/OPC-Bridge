"""Secure configuration and service identity setup helper for OPC-Bridge Windows Agent.

Enforces:
1. Fail-closed if auth_token is missing.
2. Credentials never accepted or exposed in command-line arguments (argv).
3. Hidden / masked input via getpass or secure environment variable (OPC_AUTH_TOKEN).
4. Restrictive file ACLs on ProgramData configuration file.
5. In-process Win32 API configuration for service account credentials (no sc.exe argv password).
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import socket
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("setup_config")

DEFAULT_CONFIG_PATH = r"C:\ProgramData\OPCBridge\agent.json"
DEFAULT_LOG_PATH = r"C:\ProgramData\OPCBridge\logs\agent.log"


def secure_read_credential(env_var: str, prompt_text: str, unattended: bool) -> str:
    """Read credential securely from environment variable, stdin, or masked prompt."""
    val = os.environ.get(env_var, "").strip()
    if val:
        return val

    if unattended:
        return ""

    # Check if input is piped through stdin
    if not sys.stdin.isatty():
        try:
            line = sys.stdin.readline().strip()
            if line:
                return line
        except Exception:
            pass

    try:
        return getpass.getpass(prompt_text).strip()
    except Exception as exc:
        logger.error("Failed to read masked input: %s", exc)
        return ""


def apply_restrictive_acls(file_path: str, service_user: str | None = None) -> bool:
    """Apply restrictive ACLs ensuring only Administrators and SYSTEM (and service user) can read/write."""
    if sys.platform != "win32":
        return True
    try:
        # Reset inheritance and grant full access to BUILTIN\Administrators and NT AUTHORITY\SYSTEM
        cmd = [
            "icacls.exe",
            file_path,
            "/inheritance:r",
            "/grant:r",
            "*S-1-5-32-544:(F)",  # Administrators
            "/grant:r",
            "*S-1-5-18:(F)",      # SYSTEM
        ]
        if service_user and service_user.lower() not in ["localsystem", "system"]:
            cmd.extend(["/grant:r", f"{service_user}:(R)"])

        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            logger.info("Applied restrictive ACLs to %s", file_path)
            return True
        else:
            logger.warning("icacls returned non-zero code: %s", res.stderr.strip())
            return False
    except Exception as exc:
        logger.warning("Failed to apply restrictive ACLs to %s: %s", file_path, exc)
        return False


def configure_service_credentials(service_name: str, service_user: str, service_password: str | None) -> bool:
    """Configure service account credentials via Win32 API without command-line exposure."""
    try:
        from opc_bridge.agent.service import configure_service_account
        return configure_service_account(service_name, service_user, service_password)
    except ImportError:
        logger.warning("Could not import opc_bridge.agent.service; trying direct win32service")
        try:
            import win32service
            hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
            try:
                hs = win32service.OpenService(hscm, service_name, win32service.SERVICE_CHANGE_CONFIG)
                try:
                    win32service.ChangeServiceConfig(
                        hs,
                        win32service.SERVICE_NO_CHANGE,
                        win32service.SERVICE_NO_CHANGE,
                        win32service.SERVICE_NO_CHANGE,
                        None,
                        None,
                        0,
                        None,
                        service_user,
                        service_password,
                        None,
                    )
                    logger.info("Service '%s' logon account successfully updated via Win32 API", service_name)
                    return True
                finally:
                    win32service.CloseServiceHandle(hs)
            finally:
                win32service.CloseServiceHandle(hscm)
        except Exception as exc:
            logger.error("Failed to configure service account credentials: %s", exc)
            return False


def main() -> None:
    parser = argparse.ArgumentParser(description="OPC-Bridge Secure Configuration Setup")
    parser.add_argument("--server-host", default="127.0.0.1", help="Central server hostname/IP")
    parser.add_argument("--server-port", type=int, default=8443, help="Central server port")
    parser.add_argument("--agent-id", default=f"{socket.gethostname()}-opc-01", help="Agent identifier")
    parser.add_argument("--ca-cert", default=None, help="Path to CA TLS certificate")
    parser.add_argument("--service-user", default="", help="Windows service logon account (empty for LocalSystem)")
    parser.add_argument("--config-file", default=DEFAULT_CONFIG_PATH, help="Path to target agent.json")
    parser.add_argument("--service-name", default="OPCBridgeAgent", help="Windows service name")
    parser.add_argument("--configure-service-user", action="store_true", help="Apply service user credentials to SCM")
    parser.add_argument("--unattended", action="store_true", help="Run unattended without interactive prompts")

    args = parser.parse_args()

    # 1. Acquire auth token securely
    auth_token = secure_read_credential(
        "OPC_AUTH_TOKEN",
        "Enter central registration credential (auth token): ",
        args.unattended,
    )
    if not auth_token:
        logger.error(
            "Fatal: Registration credential (auth_token) is required. "
            "Please provide via OPC_AUTH_TOKEN environment variable or interactive prompt. Failing closed."
        )
        sys.exit(1)

    # 2. Acquire service password if dedicated user specified
    service_password = None
    if args.service_user and args.service_user.lower() not in ["localsystem", "system"]:
        service_password = secure_read_credential(
            "OPC_SERVICE_PASSWORD",
            f"Enter password for service account '{args.service_user}': ",
            args.unattended,
        )

    # 3. Create target directory
    config_dir = os.path.dirname(os.path.abspath(args.config_file))
    log_dir = os.path.dirname(os.path.abspath(DEFAULT_LOG_PATH))
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    # 4. Write agent.json with central-server controlled defaults
    config_payload = {
        "server_host": args.server_host,
        "server_port": args.server_port,
        "agent_id": args.agent_id,
        "auth_token": auth_token,
        "certfile": args.ca_cert,
        "opc_prog_id": "",  # Managed dynamically via central CONFIG_PUSH
        "log_file": DEFAULT_LOG_PATH,
        "log_level": "INFO",
    }

    try:
        with open(args.config_file, "w", encoding="utf-8") as f:
            json.dump(config_payload, f, indent=2)
        logger.info("Wrote secure configuration to %s", args.config_file)
    except Exception as exc:
        logger.error("Failed to write config file %s: %s", args.config_file, exc)
        sys.exit(1)

    # 5. Apply restrictive ACLs
    apply_restrictive_acls(args.config_file, args.service_user or None)

    # 6. Configure service user credentials if requested
    if args.configure_service_user and args.service_user:
        logger.info("Configuring service logon account context: %s", args.service_user)
        success = configure_service_credentials(args.service_name, args.service_user, service_password)
        if not success:
            logger.warning("Could not set service credentials via Win32 API. Manual configuration may be needed.")

    logger.info("Configuration completed successfully.")


if __name__ == "__main__":
    main()
