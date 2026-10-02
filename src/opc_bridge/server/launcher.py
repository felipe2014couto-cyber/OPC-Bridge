"""Production command-line launcher for the central BridgeServer."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import ssl
import sys
from dataclasses import dataclass
from typing import Mapping

from opc_bridge.server.core import BridgeServer, ServerConfig
from opc_bridge.server.persistence import Database

_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8443
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class LauncherConfigurationError(ValueError):
    """Raised when production launcher settings are missing or unsafe."""


@dataclass
class LauncherSettings:
    server_config: ServerConfig
    log_level: int
    log_format: str


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value or not value.strip():
        raise LauncherConfigurationError(f"{name} must be set")
    if value != value.strip():
        raise LauncherConfigurationError(f"{name} must not have surrounding whitespace")
    return value


def _port(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, str(default))
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise LauncherConfigurationError(f"{name} must be an integer") from None
    if not 1 <= port <= 65535:
        raise LauncherConfigurationError(f"{name} must be between 1 and 65535")
    return port


def _validate_tls(certfile: str, keyfile: str) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certfile, keyfile)
    except (OSError, ssl.SSLError, ValueError):
        raise LauncherConfigurationError(
            "OPC_BRIDGE_TLS_CERTFILE and OPC_BRIDGE_TLS_KEYFILE must identify a valid TLS pair"
        ) from None


def load_settings(environ: Mapping[str, str]) -> LauncherSettings:
    """Validate environment settings completely before any listener is opened."""
    host = environ.get("OPC_BRIDGE_HOST", _DEFAULT_HOST)
    if not host or not host.strip() or host != host.strip():
        raise LauncherConfigurationError("OPC_BRIDGE_HOST must be a non-empty hostname or address")

    port = _port(environ, "OPC_BRIDGE_PORT", _DEFAULT_PORT)
    database_url = _required(environ, "DATABASE_URL")
    if "\n" in database_url or "\r" in database_url:
        raise LauncherConfigurationError("DATABASE_URL must be a PostgreSQL URL")
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise LauncherConfigurationError("DATABASE_URL must use PostgreSQL")

    certfile = _required(environ, "OPC_BRIDGE_TLS_CERTFILE")
    keyfile = _required(environ, "OPC_BRIDGE_TLS_KEYFILE")
    _validate_tls(certfile, keyfile)

    log_level_name = environ.get("OPC_BRIDGE_LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper()
    log_level = getattr(logging, log_level_name, None)
    if not isinstance(log_level, int) or log_level_name not in {
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    }:
        raise LauncherConfigurationError(
            "OPC_BRIDGE_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
        )
    log_format = environ.get("OPC_BRIDGE_LOG_FORMAT", _DEFAULT_LOG_FORMAT)
    if not log_format or "\n" in log_format or "\r" in log_format:
        raise LauncherConfigurationError("OPC_BRIDGE_LOG_FORMAT must be a non-empty single line")
    try:
        logging.Formatter(log_format)
    except (TypeError, ValueError):
        raise LauncherConfigurationError("OPC_BRIDGE_LOG_FORMAT is invalid") from None

    admin_token = environ.get("ADMIN_API_TOKEN")
    if admin_token is not None:
        if not admin_token or admin_token != admin_token.strip():
            raise LauncherConfigurationError(
                "ADMIN_API_TOKEN must be non-empty without surrounding whitespace"
            )
        _port(environ, "ADMIN_API_PORT", 8081)

    database = Database(database_url)
    return LauncherSettings(
        ServerConfig(
            host=host,
            port=port,
            certfile=certfile,
            keyfile=keyfile,
            persistence=database,
        ),
        log_level,
        log_format,
    )


def configure_logging(settings: LauncherSettings) -> None:
    """Send process logs to stdout so systemd captures them in the journal."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(settings.log_format))
    root.addHandler(handler)
    root.setLevel(settings.log_level)


async def run_server(server: BridgeServer) -> None:
    """Run until SIGINT/SIGTERM and close the server cleanly."""
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopped.set)
        except (NotImplementedError, RuntimeError):
            pass
    await server.start()
    try:
        await stopped.wait()
    finally:
        await server.stop()


def main() -> int:
    try:
        settings = load_settings(os.environ)
    except LauncherConfigurationError as exc:
        print(f"opc-bridge-server configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings)
    try:
        server = BridgeServer(settings.server_config)
        asyncio.run(run_server(server))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - keep driver errors and secrets out of logs
        # Driver exceptions can contain connection details; never print them verbatim.
        logging.getLogger(__name__).error(
            "BridgeServer stopped during startup or operation (%s)", type(exc).__name__
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
