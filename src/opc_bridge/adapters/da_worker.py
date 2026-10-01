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

# Configure basic logging for child process
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DAWorker pid=%(process)d] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

IPC_AUTHKEY = b"opc-bridge-supervised-worker"


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
            if op == "ping":
                conn.send({"ok": True, "pong": True, "time": time.time()})
            elif op == "connect":
                prog_id = msg["prog_id"]
                adapter.connect(prog_id)
                conn.send({"ok": True})
            elif op == "disconnect":
                adapter.disconnect()
                conn.send({"ok": True})
            elif op == "create_group":
                name = msg["name"]
                update_rate_ms = msg["update_rate_ms"]
                handle = adapter.create_group(name, update_rate_ms)
                conn.send({"ok": True, "handle": handle})
            elif op == "remove_group":
                handle = msg["handle"]
                adapter.remove_group(handle)
                conn.send({"ok": True})
            elif op == "add_items":
                group = msg["group"]
                item_paths = msg["item_paths"]
                mapping = adapter.add_items(group, item_paths)
                conn.send({"ok": True, "mapping": mapping})
            elif op == "remove_items":
                group = msg["group"]
                item_ids = msg["item_ids"]
                adapter.remove_items(group, item_ids)
                conn.send({"ok": True})
            elif op == "read_device":
                group = msg["group"]
                item_ids = msg["item_ids"]
                results = adapter.read_device(group, item_ids)
                conn.send({"ok": True, "results": results})
            elif op == "browse_items":
                parent_path = msg.get("parent_path", "")
                entries = adapter.browse_items(parent_path)
                conn.send({"ok": True, "entries": entries})
            elif op == "get_server_status":
                status = adapter.get_server_status()
                conn.send({"ok": True, "status": status})
            elif op == "discover_servers":
                servers = OpcDaAdapter.discover_servers()
                conn.send({"ok": True, "servers": servers})
            elif op == "crash":
                # For testing crash recovery
                logger.warning("Simulated crash requested; terminating abruptly.")
                os._exit(42)
            elif op == "exit":
                logger.info("Graceful exit requested.")
                running = False
                conn.send({"ok": True})
                break
            else:
                conn.send({"ok": False, "error": f"Unknown operation: {op}"})
        except Exception as exc:
            logger.exception("Error executing operation %r", op)
            conn.send({"ok": False, "error": str(exc), "type": type(exc).__name__})

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
