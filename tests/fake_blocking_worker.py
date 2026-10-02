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
    except Exception:
        logger.exception("Fake worker connection failed")
        sys.exit(1)

    from opc_bridge.adapters.da import DevelopmentComServer, OpcDaAdapter
    from opc_bridge.adapters.da_worker import process_worker_command

    adapter = OpcDaAdapter(default_prog_id=args.prog_id, com_factory=DevelopmentComServer)
    if args.prog_id:
        try:
            adapter.connect(args.prog_id)
        except Exception:
            logger.debug("Fake worker adapter initialization failed", exc_info=True)

    running = True
    while running:
        try:
            msg = conn.recv()
        except Exception:  # noqa: BLE001 - best-effort provider cleanup.
            break

        op = msg.get("op")
        try:
            if op == "crash":
                os._exit(42)
            # If test hang environment variable is enabled, hang indefinitely.
            if op == "read_device" and os.environ.get("TEST_HANG_ON_READ") == "1":
                logger.warning("TEST_HANG_ON_READ=1 detected; simulating indefinite driver block...")
                while True:
                    time.sleep(1.0)
            conn.send(process_worker_command(adapter, msg))
            if op == "exit":
                running = False
                break
        except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
            conn.send({"ok": False, "error": str(exc), "type": type(exc).__name__})

    try:
        conn.close()
    except Exception:
        logger.debug("Fake worker IPC close failed", exc_info=True)


if __name__ == "__main__":
    main()
