"""Test-only worker module for SupervisedOpcAdapter fault simulation.

Used strictly in tests to simulate an unkillable COM driver hang during read_device
without putting any sentinel or test hooks into production adapter code.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from multiprocessing.connection import Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FakeWorker pid=%(process)d] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

IPC_AUTHKEY = b"opc-bridge-supervised-worker"


def main() -> None:
    parser = argparse.ArgumentParser(description="Test-only Fake OPC Worker")
    parser.add_argument("--pipe", required=True, help="IPC host:port or pipe")
    parser.add_argument("--prog-id", default=None, help="OPC ProgID")
    args = parser.parse_args()

    pipe_arg = args.pipe
    if ":" in pipe_arg and not pipe_arg.startswith("\\\\"):
        host, port_str = pipe_arg.split(":", 1)
        address = (host, int(port_str))
        family = "AF_INET"
    else:
        address = pipe_arg
        family = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"

    logger.info("Connecting fake test worker to supervisor at %s...", address)
    try:
        conn = Client(address, family=family, authkey=IPC_AUTHKEY)
    except Exception as exc:
        logger.exception("Fake worker connection failed: %s", exc)
        sys.exit(1)

    from opc_bridge.adapters.da import OpcDaAdapter

    adapter = OpcDaAdapter(default_prog_id=args.prog_id)
    if args.prog_id:
        try:
            adapter.connect(args.prog_id)
        except Exception:
            pass

    running = True
    while running:
        try:
            msg = conn.recv()
        except Exception:
            break

        op = msg.get("op")
        try:
            if op == "ping":
                conn.send({"ok": True, "pong": True, "time": time.time()})
            elif op == "connect":
                adapter.connect(msg["prog_id"])
                conn.send({"ok": True})
            elif op == "disconnect":
                adapter.disconnect()
                conn.send({"ok": True})
            elif op == "create_group":
                handle = adapter.create_group(msg["name"], msg["update_rate_ms"])
                conn.send({"ok": True, "handle": handle})
            elif op == "remove_group":
                adapter.remove_group(msg["handle"])
                conn.send({"ok": True})
            elif op == "add_items":
                mapping = adapter.add_items(msg["group"], msg["item_paths"])
                conn.send({"ok": True, "mapping": mapping})
            elif op == "remove_items":
                adapter.remove_items(msg["group"], msg["item_ids"])
                conn.send({"ok": True})
            elif op == "read_device":
                # If test hang environment variable is enabled, hang indefinitely
                if os.environ.get("TEST_HANG_ON_READ") == "1":
                    logger.warning("TEST_HANG_ON_READ=1 detected; simulating indefinite driver block...")
                    while True:
                        time.sleep(1.0)
                results = adapter.read_device(msg["group"], msg["item_ids"])
                conn.send({"ok": True, "results": results})
            elif op == "browse_items":
                entries = adapter.browse_items(msg.get("parent_path", ""))
                conn.send({"ok": True, "entries": entries})
            elif op == "get_server_status":
                status = adapter.get_server_status()
                conn.send({"ok": True, "status": status})
            elif op == "discover_servers":
                servers = OpcDaAdapter.discover_servers()
                conn.send({"ok": True, "servers": servers})
            elif op == "crash":
                os._exit(42)
            elif op == "exit":
                running = False
                conn.send({"ok": True})
                break
            else:
                conn.send({"ok": False, "error": f"Unknown op: {op}"})
        except Exception as exc:
            conn.send({"ok": False, "error": str(exc), "type": type(exc).__name__})

    try:
        conn.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
