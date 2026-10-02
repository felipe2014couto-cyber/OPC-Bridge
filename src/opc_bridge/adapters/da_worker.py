"""Supervised child process worker for COM OPC DA operations.

Runs in an isolated child process to protect the main Windows service
from COM/DLL threading errors, memory access violations, or unhandled crashes.
Communicates with SupervisedOpcAdapter via multiprocessing connection pipes/sockets.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from multiprocessing.connection import Client

from opc_bridge.adapters.base import group_handle_from_ipc, group_handle_to_ipc, opc_hresult

# Configure basic logging for child process
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DAWorker pid=%(process)d] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

IPC_AUTHKEY = b"opc-bridge-supervised-worker"


def worker_error_response(exc: Exception) -> dict:
    """Preserve a provider HRESULT without serializing the provider exception."""
    return {"ok": False, "error": str(exc), "type": type(exc).__name__,
            "hresult": opc_hresult(exc)}


def process_worker_command(adapter, msg: dict) -> dict:
    """Execute a command and return data safe to serialize across IPC."""
    op = msg.get("op")
    if op == "ping":
        return {"ok": True, "pong": True, "time": time.time()}
    if op == "connect":
        adapter.connect(msg["prog_id"])
        return {"ok": True}
    if op == "disconnect":
        adapter.disconnect()
        return {"ok": True}
    if op == "create_group":
        handle = adapter.create_group(msg["name"], msg["update_rate_ms"])
        return {"ok": True, "handle": group_handle_to_ipc(handle)}
    if op == "remove_group":
        adapter.remove_group(group_handle_from_ipc(msg["handle"]))
        return {"ok": True}
    if op == "add_items":
        group = group_handle_from_ipc(msg["group"])
        return {"ok": True, "mapping": adapter.add_items(group, msg["item_paths"])}
    if op == "remove_items":
        group = group_handle_from_ipc(msg["group"])
        adapter.remove_items(group, msg["item_ids"])
        return {"ok": True}
    if op == "read_device":
        group = group_handle_from_ipc(msg["group"])
        return {"ok": True, "results": adapter.read_device(group, msg["item_ids"])}
    if op == "browse_items":
        return {"ok": True, "entries": adapter.browse_items(msg.get("parent_path", ""))}
    if op == "get_server_status":
        return {"ok": True, "status": adapter.get_server_status()}
    if op == "discover_servers":
        from opc_bridge.adapters.da import OpcDaAdapter

        return {"ok": True, "servers": OpcDaAdapter.discover_servers()}
    if op == "exit":
        return {"ok": True}
    return {"ok": False, "error": f"Unknown operation: {op}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="OPC DA Isolated COM Worker")
    parser.add_argument("--pipe", required=True, help="IPC Pipe or host:port address for supervisor connection")
    parser.add_argument("--prog-id", default=None, help="Initial ProgID to connect to")
    args = parser.parse_args()

    # Determine address and family
    pipe_arg = args.pipe
    if ":" in pipe_arg and not pipe_arg.startswith("\\\\"):
        host, port_str = pipe_arg.split(":", 1)
        address = (host, int(port_str))
        family = "AF_INET"
    elif pipe_arg.startswith("\\\\"):
        address = pipe_arg
        family = "AF_PIPE"
    else:
        address = pipe_arg
        family = "AF_UNIX" if sys.platform != "win32" else "AF_PIPE"

    logger.info("Connecting to supervisor at %s (family=%s)...", address, family)
    try:
        conn = Client(address, family=family, authkey=IPC_AUTHKEY)
        logger.info("Connected to supervisor successfully.")
    except Exception:
        logger.exception("Failed to connect to supervisor")
        sys.exit(1)

    # Initialize COM if on Windows
    if sys.platform == "win32":
        try:
            import pythoncom
            pythoncom.CoInitialize()
            logger.info("COM CoInitialize() completed in worker process.")
        except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
            logger.warning("Could not initialize COM in worker: %s", exc)

    from opc_bridge.adapters.da import OpcDaAdapter

    adapter = OpcDaAdapter(default_prog_id=args.prog_id)
    if args.prog_id:
        try:
            adapter.connect(args.prog_id)
        except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
            logger.warning("Initial connect to %s failed: %s", args.prog_id, exc)

    # Main command processing loop
    running = True
    while running:
        try:
            msg = conn.recv()
        except (EOFError, BrokenPipeError, ConnectionResetError):
            logger.info("Supervisor closed connection. Exiting.")
            break
        except Exception:
            logger.exception("Error receiving command")
            break

        op = msg.get("op")
        try:
            if op == "crash":
                logger.warning("Simulated crash requested; terminating abruptly.")
                os._exit(42)
            conn.send(process_worker_command(adapter, msg))
            if op == "exit":
                logger.info("Graceful exit requested.")
                running = False
                break
        except Exception as exc:
            logger.exception("Error executing operation %r", op)
            conn.send(worker_error_response(exc))

    try:
        adapter.disconnect()
    except Exception:
        logger.debug("Worker adapter cleanup failed", exc_info=True)

    if sys.platform == "win32":
        try:
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:
            logger.debug("Worker COM uninitialization failed", exc_info=True)

    try:
        conn.close()
    except Exception:
        logger.debug("Worker IPC close failed", exc_info=True)
    logger.info("OPC Worker process stopped.")


if __name__ == "__main__":
    main()
