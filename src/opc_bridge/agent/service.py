"""Windows Service implementation for OPC-Bridge Agent (T8).

Provides:
- Automatic startup (SERVICE_AUTO_START)
- SCM Failure Actions (restart on 1st/2nd/subsequent failure)
- Clean shutdown via SvcStop
- Supervision of the central server TLS connection and COM worker child process
- Standalone / interactive foreground run mode (--run)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
from typing import Any

from opc_bridge.adapters.supervised import SupervisedOpcAdapter
from opc_bridge.agent.client import AgentClient
from opc_bridge.agent.supervisor import AgentSupervisor, configure_rotating_logging

logger = logging.getLogger("opc_bridge.service")

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


SERVICE_NAME = "OPCBridgeAgent"
SERVICE_DISPLAY_NAME = "OPC-Bridge Agent Service"
SERVICE_DESCRIPTION = (
    "High-frequency OPC DA Bridge Agent for ABB/industrial telemetry collection."
)


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load agent configuration from file or defaults."""
    default_config: dict[str, Any] = {
        "server_host": "127.0.0.1",
        "server_port": 8443,
        "agent_id": "opc-agent-windows-01",
        "auth_token": "opc-bridge-secret-token",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "update_rate_ms": 1000,
        "certfile": None,
        "log_file": "agent.log",
        "log_level": "INFO",
    }

    search_paths = []
    if config_path:
        search_paths.append(config_path)
    if os.environ.get("OPC_BRIDGE_CONFIG"):
        search_paths.append(os.environ["OPC_BRIDGE_CONFIG"])
    search_paths.extend([
        os.path.join(os.getcwd(), "config", "agent.json"),
        os.path.join(os.getcwd(), "agent.json"),
        os.path.join("C:\\ProgramData", "OPCBridge", "agent.json"),
    ])

    for path in search_paths:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                    default_config.update(user_cfg)
                logger.info("Loaded configuration from %s", path)
                break
            except Exception as exc:
                logger.warning("Failed to parse config file %s: %s", path, exc)

    return default_config


def configure_service_recovery(service_name: str = SERVICE_NAME) -> bool:
    """Configure Windows SCM failure recovery actions via sc.exe.

    Configures SCM to automatically restart the service after 5s on 1st crash,
    10s on 2nd crash, and 60s on subsequent crashes, resetting counter daily.
    """
    if sys.platform != "win32":
        return False
    try:
        cmd = [
            "sc.exe",
            "failure",
            service_name,
            "reset=",
            "86400",
            "actions=",
            "restart/5000/restart/10000/restart/60000",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            logger.info("SCM failure recovery configured successfully for %s", service_name)
            return True
        else:
            logger.warning("sc.exe failure returned non-zero code: %s", res.stderr.strip())
            return False
    except Exception as exc:
        logger.warning("Failed to configure service recovery via sc.exe: %s", exc)
        return False


def run_agent_main(config: dict[str, Any], stop_event: asyncio.Event | None = None) -> None:
    """Core agent runtime loop using SupervisedOpcAdapter and AgentSupervisor."""
    log_file = config.get("log_file", "agent.log")
    log_level_name = config.get("log_level", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    log_dir = os.path.dirname(os.path.abspath(log_file))
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

    configure_rotating_logging(log_file, level=log_level)
    logger.info("Starting OPC-Bridge Agent with config: %s", config)

    prog_id = config.get("opc_prog_id", "ABB.AfwOpcDaSurrogate.1")
    adapter = SupervisedOpcAdapter(prog_id=prog_id)

    raw_token = config.get("auth_token", "default-token")
    auth_token_hash = hashlib.sha256(raw_token.encode("utf-8")).digest()

    def client_factory() -> AgentClient:
        return AgentClient(
            server_host=config["server_host"],
            server_port=int(config["server_port"]),
            agent_id=config["agent_id"],
            auth_token_hash=auth_token_hash,
            adapter=adapter,
            certfile=config.get("certfile"),
        )

    supervisor = AgentSupervisor(client_factory=client_factory)

    async def main_async() -> None:
        supervisor_task = asyncio.create_task(supervisor.run())
        if stop_event:
            stop_waiter = asyncio.create_task(stop_event.wait())
            _, pending = await asyncio.wait(
                [supervisor_task, stop_waiter],
                return_when=asyncio.FIRST_COMPLETED,
            )
            supervisor.stop()
            for t in pending:
                t.cancel()
        else:
            await supervisor_task

    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Agent shutdown requested.")
    finally:
        try:
            adapter.disconnect()
        except Exception:
            pass
        logger.info("Agent stopped cleanly.")


if HAS_WIN32:

    class OpcBridgeWindowsService(win32serviceutil.ServiceFramework):
        """Windows Service implementation for OPC-Bridge Agent."""

        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args: list[str]) -> None:
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._async_stop_event = asyncio.Event()

        def SvcStop(self) -> None:
            """Signal service stop to SCM and cancel agent loops."""
            logger.info("Service stop requested by SCM.")
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            self._async_stop_event.set()

        def SvcDoRun(self) -> None:
            """Main service execution called by SCM."""
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            logger.info("Windows Service %s started.", self._svc_name_)
            config = load_config()
            run_agent_main(config, stop_event=self._async_stop_event)
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STOPPED,
                (self._svc_name_, ""),
            )
            logger.info("Windows Service %s stopped.", self._svc_name_)

else:

    class OpcBridgeWindowsService:  # type: ignore
        """Dummy fallback when pywin32 is not installed (e.g. Linux)."""



def main() -> None:
    parser = argparse.ArgumentParser(description="OPC-Bridge Agent Service Manager")
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run agent in interactive console mode (foreground)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to JSON configuration file",
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=["install", "start", "stop", "restart", "remove"],
        help="Service management command",
    )

    args, unknown = parser.parse_known_args()

    if args.run:
        config = load_config(args.config)
        print("Starting OPC-Bridge Agent in foreground console mode...")
        run_agent_main(config)
        return

    if sys.platform != "win32" or not HAS_WIN32:
        if args.action:
            print(f"Windows Service action '{args.action}' is only available on Windows with pywin32.")
            sys.exit(1)
        # Default to foreground run on Linux
        config = load_config(args.config)
        run_agent_main(config)
        return

    if args.action == "install":
        print(f"Installing Windows Service {SERVICE_NAME} (SERVICE_AUTO_START)...")
        # Install with automatic startup
        win32serviceutil.HandleCommandLine(
            OpcBridgeWindowsService,
            argv=[sys.argv[0], "--startup=auto", "install"],
        )
        print("Configuring SCM failure recovery actions...")
        configure_service_recovery(SERVICE_NAME)
        print("Installation complete.")
    elif args.action in ["start", "stop", "restart", "remove"]:
        win32serviceutil.HandleCommandLine(
            OpcBridgeWindowsService,
            argv=[sys.argv[0], args.action] + unknown,
        )
    else:
        # Default behavior: pass to ServiceFramework or print help
        if len(sys.argv) > 1 and sys.argv[1] not in ["--run", "--config"]:
            win32serviceutil.HandleCommandLine(OpcBridgeWindowsService)
        else:
            parser.print_help()


if __name__ == "__main__":
    main()
