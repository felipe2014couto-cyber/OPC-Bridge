"""Secure configuration and service identity setup helper for OPC-Bridge Windows Agent.

Enforces:
1. Fail-closed if auth_token is missing.
2. Credentials never accepted or exposed in command-line arguments (argv).
3. Hidden / masked input via getpass, secure environment variable (OPC_AUTH_TOKEN),
   or bundled bootstrap configuration (agent.bootstrap.json).
4. Restrictive file ACLs on ProgramData configuration and CA certificate files.
5. In-process Win32 API configuration for service account credentials (no sc.exe argv password).
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import socket
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("setup_config")

DEFAULT_CONFIG_PATH = r"C:\ProgramData\OPCBridge\agent.json"
DEFAULT_LOG_PATH = r"C:\ProgramData\OPCBridge\logs\agent.log"
DEFAULT_CA_PATH = r"C:\ProgramData\OPCBridge\ca.pem"


def secure_read_credential(
    env_var: str, prompt_text: str, unattended: bool, bootstrap_val: str = ""
) -> str:
    """Read credential securely from environment variable, bootstrap config, stdin, or masked prompt."""
    val = os.environ.get(env_var, "").strip()
    if val:
        return val

    if bootstrap_val:
        return bootstrap_val.strip()

    if unattended:
        return ""

    # Check if input is piped through stdin
    if not sys.stdin.isatty():
        try:
            line = sys.stdin.readline().strip()
            if line:
                return line
        except (OSError, ValueError):
            logger.debug("Credential stdin is unavailable")

    try:
        return getpass.getpass(prompt_text).strip()
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("Failed to read masked input: %s", exc)
        return ""


def apply_restrictive_acls(file_path: str, service_user: str | None = None) -> bool:
    """Apply restrictive ACLs ensuring only Administrators and SYSTEM (and service user) can read/write."""
    if sys.platform != "win32":
        return True
    try:
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
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("Failed to apply restrictive ACLs to %s: %s", file_path, exc)
        return False


def configure_service_credentials(
    service_name: str, service_user: str, service_password: str | None
) -> bool:
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
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Failed to configure service account credentials: %s", exc)
            return False


def load_bootstrap(bootstrap_path: str | None) -> dict[str, str]:
    """Load bootstrap configuration with fallback candidate paths."""
    candidates = []
    if bootstrap_path:
        candidates.append(bootstrap_path)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.extend([
        os.path.join(script_dir, "config", "agent.bootstrap.json"),
        os.path.join(script_dir, "agent.bootstrap.json"),
        os.path.join(os.getcwd(), "config", "agent.bootstrap.json"),
        os.path.join(os.getcwd(), "agent.bootstrap.json"),
    ])

    for cand in candidates:
        if os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info("Loaded bootstrap configuration from %s", cand)
                return data
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("Could not parse bootstrap file %s: %s", cand, exc)

    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="OPC-Bridge Secure Configuration Setup")
    parser.add_argument("--server-host", default=None, help="Central server hostname/IP")
    parser.add_argument("--server-port", type=int, default=None, help="Central server port")
    parser.add_argument("--server-hostname", default=None, help="Central server TLS hostname")
    parser.add_argument("--agent-id", default=None, help="Agent identifier")
    parser.add_argument("--ca-cert", default=None, help="Path to CA TLS certificate")
    parser.add_argument("--bootstrap-file", default=None, help="Path to bootstrap configuration file")
    parser.add_argument("--service-user", default="", help="Windows service logon account (empty for LocalSystem)")
    parser.add_argument("--config-file", default=DEFAULT_CONFIG_PATH, help="Path to target agent.json")
    parser.add_argument("--service-name", default="OPCBridgeAgent", help="Windows service name")
    parser.add_argument("--configure-service-user", action="store_true", help="Apply service user credentials to SCM")
    parser.add_argument("--unattended", action="store_true", help="Run unattended without interactive prompts")

    parser.add_argument("--worker-architecture", choices=("auto", "x64", "x86"), default="auto")
    args = parser.parse_args()
    from opc_bridge.adapters.worker_runtime import select_worker_runtime

    selected, _ = select_worker_runtime(args.worker_architecture, sys.executable)
    logger.info("Worker architecture: requested=%s selected=%s", args.worker_architecture, selected)

    # Load bootstrap values if available
    bootstrap = load_bootstrap(args.bootstrap_file)

    server_host = args.server_host or bootstrap.get("server_host") or "127.0.0.1"
    server_port = args.server_port or bootstrap.get("server_port") or 8443
    server_hostname = args.server_hostname or bootstrap.get("server_hostname") or "localhost"
    agent_id = args.agent_id or bootstrap.get("agent_id") or f"{socket.gethostname()}-opc-01"
    bootstrap_token = bootstrap.get("auth_token", "")
    ca_cert_candidate = args.ca_cert or bootstrap.get("ca_cert")

    # 1. Acquire auth token securely
    auth_token = secure_read_credential(
        "OPC_AUTH_TOKEN",
        "Enter central registration credential (auth token): ",
        args.unattended,
        bootstrap_val=bootstrap_token,
    )
    if not auth_token:
        logger.error(
            "Fatal: Registration credential (auth_token) is required. "
            "Please provide via OPC_AUTH_TOKEN environment variable, bootstrap config, or interactive prompt. Failing closed."
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

    # Attach install.log FileHandler
    install_log_path = os.path.join(log_dir, "install.log")
    try:
        fh = logging.FileHandler(install_log_path, mode="a", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [setup_config] %(message)s"))
        logger.addHandler(fh)
    except OSError as log_exc:
        logger.warning("Could not attach FileHandler to %s: %s", install_log_path, log_exc)

    # Copy diagnostics tools to ProgramData directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for diag_file in ["diagnostics.bat", "diagnostics.py"]:
        src_diag = os.path.join(script_dir, diag_file)
        if os.path.isfile(src_diag):
            dst_diag = os.path.join(config_dir, diag_file)
            try:
                shutil.copy2(src_diag, dst_diag)
                logger.info("Installed %s to %s", diag_file, dst_diag)
            except OSError as copy_exc:
                logger.warning("Could not copy %s to %s: %s", diag_file, dst_diag, copy_exc)

    # 4. Handle CA Certificate copy to persistent directory
    final_ca_path: str | None = None
    if ca_cert_candidate:
        cand_paths = [
            ca_cert_candidate,
            os.path.join(script_dir, ca_cert_candidate),
            os.path.join(os.getcwd(), ca_cert_candidate),
            os.path.join(script_dir, "config", os.path.basename(ca_cert_candidate)),
        ]
        found_ca = None
        for p in cand_paths:
            if os.path.isfile(p):
                found_ca = p
                break

        if found_ca:
            try:
                shutil.copy2(found_ca, DEFAULT_CA_PATH)
                apply_restrictive_acls(DEFAULT_CA_PATH, args.service_user or None)
                final_ca_path = DEFAULT_CA_PATH
                logger.info("Copied CA certificate from %s to %s", found_ca, DEFAULT_CA_PATH)
            except (OSError, ValueError, RuntimeError) as exc:
                logger.warning("Could not copy CA certificate to %s: %s; using original path", DEFAULT_CA_PATH, exc)
                final_ca_path = found_ca
        else:
            logger.warning("Specified CA certificate was not found: %s", ca_cert_candidate)
    elif os.path.isfile(DEFAULT_CA_PATH):
        final_ca_path = DEFAULT_CA_PATH

    # 5. Write agent.json with central-server controlled defaults
    config_payload = {
        "server_host": server_host,
        "server_port": int(server_port),
        "server_hostname": server_hostname,
        "agent_id": agent_id,
        "auth_token": auth_token,
        "certfile": final_ca_path,
        "opc_prog_id": "",  # Managed dynamically via central CONFIG_PUSH
        "worker_architecture": args.worker_architecture,
        "log_file": DEFAULT_LOG_PATH,
        "log_level": "INFO",
    }

    safe_summary = {
        k: ("[REDACTED]" if any(s in k.lower() for s in ["token", "password", "secret", "key"]) else v)
        for k, v in config_payload.items()
    }
    logger.info("Configuring agent with parameters: %s", safe_summary)

    try:
        with open(args.config_file, "w", encoding="utf-8") as f:
            json.dump(config_payload, f, indent=2)
        logger.info("Wrote secure configuration to %s", args.config_file)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("Failed to write config file %s: %s", args.config_file, exc)
        sys.exit(1)

    # 6. Apply restrictive ACLs
    apply_restrictive_acls(args.config_file, args.service_user or None)

    # 7. Configure service user credentials if requested
    if args.configure_service_user and args.service_user:
        logger.info("Configuring service logon account context: %s", args.service_user)
        success = configure_service_credentials(args.service_name, args.service_user, service_password)
        if not success:
            logger.warning("Could not set service credentials via Win32 API. Manual configuration may be needed.")

    logger.info("Configuration completed successfully.")


if __name__ == "__main__":
    main()
