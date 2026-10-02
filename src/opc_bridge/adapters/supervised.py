"""Supervised OPC DA adapter with out-of-process COM execution and auto-recovery.

Shields the main agent service from COM apartment issues, DLL memory crashes,
and vendor driver hangs by running OpcDaAdapter in an isolated child process.
Detects process crashes, hung calls (watchdog timeout), and automatically
restarts the child process and restores all active groups and items transparently.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from multiprocessing.connection import Listener
from typing import Any

from opc_bridge.adapters.base import (
    BrowseEntry,
    GroupHandle,
    ServerStatus,
    group_handle_from_ipc,
    group_handle_to_ipc,
)
from opc_bridge.adapters.da_worker import IPC_AUTHKEY
from opc_bridge.adapters.worker_runtime import select_worker_runtime
from opc_bridge.protocol import ItemResult, ItemStatus, ValueType

logger = logging.getLogger(__name__)


class SupervisedOpcAdapter:
    """Supervised OPC DA adapter running COM in an isolated child process.

    Implements the opc-adapter.md contract with strict per-request deadline
    enforcement, immediate termination/reaping of blocked worker processes,
    and automatic state restoration.
    """

    def __init__(
        self,
        prog_id: str | None = None,
        worker_executable: str | None = None,
        worker_module: str = "opc_bridge.adapters.da_worker",
        command_timeout: float = 10.0,
        connect_timeout: float = 15.0,
        worker_architecture: str | None = None,
    ) -> None:
        if not worker_executable:
            exe = sys.executable
            if exe.lower().endswith("pythonservice.exe"):
                candidate = os.path.join(os.path.dirname(exe), "python.exe")
                if os.path.exists(candidate):
                    exe = candidate
            self.worker_executable = exe
        else:
            self.worker_executable = worker_executable
        self.worker_architecture = worker_architecture
        if worker_architecture is not None:
            if worker_executable is not None:
                raise ValueError("Architecture selection requires bundled runtimes")
            self.worker_architecture, self.worker_executable = select_worker_runtime(
                worker_architecture, self.worker_executable
            )
        self.worker_module = worker_module
        self.command_timeout = command_timeout
        self.connect_timeout = connect_timeout
        self._prog_id = prog_id

        # Desired state tracking for automatic recovery
        self._groups: dict[str, int] = {}  # group_name -> update_rate_ms
        self._group_items: dict[str, list[str]] = {}  # group_name -> [item_path, ...]
        self._group_handles: dict[str, GroupHandle] = {}
        self._item_mappings: dict[str, dict[str, int]] = {}  # group_name -> {path: item_id}

        self._process: subprocess.Popen[Any] | None = None
        self._conn: Any = None
        self._connected = False
        self._recovering = False

    @property
    def is_alive(self) -> bool:
        """Check if child worker process is currently running."""
        return self._process is not None and self._process.poll() is None

    @property
    def worker_pid(self) -> int | None:
        """Return the PID of the active worker process, if alive."""
        if self._process is not None and self._process.poll() is None:
            return self._process.pid
        return None

    def _start_worker(self) -> None:
        """Launch the worker subprocess and establish IPC connection."""
        self._cleanup_process()

        # Create local TCP listener for IPC
        listener = Listener(("127.0.0.1", 0), family="AF_INET", authkey=IPC_AUTHKEY)
        host, port = listener.address
        pipe_arg = f"{host}:{port}"

        cmd = [
            self.worker_executable,
            "-m",
            self.worker_module,
            "--pipe",
            pipe_arg,
        ]
        if self._prog_id:
            cmd.extend(["--prog-id", self._prog_id])

        logger.info("Spawning OPC worker process: %s", " ".join(cmd))
        env = os.environ.copy()
        # Add src to PYTHONPATH if not already present
        src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        existing_pythonpath = env.get("PYTHONPATH", "")
        if src_dir not in existing_pythonpath:
            env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else src_dir

        self._process = subprocess.Popen(
            cmd,
            env=env,
            stdout=None,
            stderr=None,
        )

        try:
            # Wait for child to connect with timeout
            if listener._listener._socket.fileno() != -1:
                self._conn = listener.accept()
                logger.info("OPC worker connected successfully pid=%s", self._process.pid)
            else:
                raise ConnectionError("Listener socket closed before connection")
        except Exception as exc:
            self._cleanup_process()
            raise ConnectionError(f"Failed to establish IPC with OPC worker: {exc}") from exc
        finally:
            listener.close()

    def _cleanup_process(self) -> None:
        """Forcibly kill and reap child process, closing IPC handles."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                logger.debug("IPC connection close failed", exc_info=True)
            self._conn = None

        if self._process is not None:
            pid = self._process.pid
            if self._process.poll() is None:
                try:
                    logger.info("Forcibly killing and reaping child process pid=%s", pid)
                    self._process.kill()
                    self._process.wait(timeout=2.0)
                except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
                    logger.debug("Error killing/reaping child process pid=%s: %s", pid, exc)
            self._process = None

    def _recover(self) -> None:
        """Restart worker process and replay state (connection, groups, items)."""
        if self._recovering:
            return
        self._recovering = True
        logger.warning("Initiating OPC worker recovery (previous pid was %s)...", self._process.pid if self._process else None)

        try:
            self._start_worker()

            # Re-connect if prog_id is configured
            if self._prog_id:
                logger.info("Recovering connection to %s...", self._prog_id)
                resp = self._send_raw({"op": "connect", "prog_id": self._prog_id}, timeout=self.connect_timeout)
                if not resp.get("ok"):
                    logger.warning("Recovery connect failed: %s", resp.get("error"))

            # Re-create active groups and re-register items
            for group_name, update_rate in list(self._groups.items()):
                logger.info("Re-creating group %s (rate=%dms)...", group_name, update_rate)
                resp = self._send_raw({"op": "create_group", "name": group_name, "update_rate_ms": update_rate})
                if resp.get("ok"):
                    handle = group_handle_from_ipc(resp["handle"])
                    self._group_handles[group_name] = handle
                    items = self._group_items.get(group_name, [])
                    if items:
                        logger.info("Re-adding %d items to group %s...", len(items), group_name)
                        add_resp = self._send_raw({
                            "op": "add_items", "group": group_handle_to_ipc(handle), "item_paths": items
                        })
                        if add_resp.get("ok"):
                            self._item_mappings[group_name] = add_resp.get("mapping", {})

            logger.info("OPC worker recovery completed successfully pid=%s", self._process.pid)
        finally:
            self._recovering = False

    def _send_raw(self, msg: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        """Send command to worker and receive response with strict deadline enforcement."""
        if self._conn is None or not self.is_alive:
            raise ConnectionError("Worker is not running")

        self._conn.send(msg)
        if not self._conn.poll(timeout):
            # Worker hung or blocked in unkillable COM call: kill and reap process immediately
            pid = self._process.pid if self._process else None
            logger.error("OPC worker call '%s' hung after %.1fs; killing and reaping pid=%s", msg.get("op"), timeout, pid)
            self._cleanup_process()
            raise TimeoutError(f"OPC worker hung executing {msg.get('op')}")

        return self._conn.recv()

    def _execute(
        self,
        msg: dict[str, Any],
        timeout: float | None = None,
        allow_retry: bool = True,
    ) -> Any:
        """Execute command on worker with auto-recovery on crash or timeout."""
        timeout = timeout or self.command_timeout
        attempts = 2 if allow_retry else 1

        for attempt in range(attempts):
            try:
                if not self.is_alive or self._conn is None:
                    self._recover()

                resp = self._send_raw(msg, timeout=timeout)
                if not resp.get("ok"):
                    err_msg = resp.get("error", "Unknown error")
                    err_type = resp.get("type", "RuntimeError")
                    if err_type == "ConnectionError":
                        raise ConnectionError(err_msg)
                    elif err_type == "ValueError":
                        raise ValueError(err_msg)
                    raise RuntimeError(err_msg)
                return resp
            except TimeoutError as exc:
                # Do not retry on timeout; re-raise immediately to enforce deadline
                logger.warning(
                    "OPC worker operation '%s' timed out (attempt %d/%d): %s; child was killed",
                    msg.get("op"), attempt + 1, attempts, exc,
                )
                raise
            except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
                logger.warning("OPC worker crash/disconnect during '%s' (attempt %d/%d): %s", msg.get("op"), attempt + 1, attempts, exc)
                self._cleanup_process()
                if attempt == attempts - 1:
                    raise
                self._recover()

    def connect(self, prog_id: str) -> None:
        """Connect to an OPC DA server by ProgID."""
        self._prog_id = prog_id
        if not self.is_alive:
            self._start_worker()

        self._execute({"op": "connect", "prog_id": prog_id}, timeout=self.connect_timeout)
        self._connected = True

    def disconnect(self) -> None:
        """Disconnect and terminate worker cleanly."""
        if self.is_alive and self._conn is not None:
            try:
                self._send_raw({"op": "disconnect"}, timeout=3.0)
                self._send_raw({"op": "exit"}, timeout=2.0)
            except Exception:
                logger.debug("Worker disconnect failed", exc_info=True)

        self._cleanup_process()
        self._connected = False
        self._groups.clear()
        self._group_items.clear()
        self._group_handles.clear()
        self._item_mappings.clear()
        logger.info("Supervised OPC adapter disconnected")

    def create_group(self, name: str, update_rate_ms: int) -> GroupHandle:
        """Create a new OPC group."""
        resp = self._execute({"op": "create_group", "name": name, "update_rate_ms": update_rate_ms})
        handle = group_handle_from_ipc(resp["handle"])
        self._groups[name] = update_rate_ms
        self._group_items[name] = []
        self._group_handles[name] = handle
        return handle

    def remove_group(self, handle: GroupHandle) -> None:
        """Remove an OPC group."""
        group_name = handle.name if hasattr(handle, "name") else str(handle)
        self._execute({"op": "remove_group", "handle": group_handle_to_ipc(handle)})
        self._groups.pop(group_name, None)
        self._group_items.pop(group_name, None)
        self._group_handles.pop(group_name, None)
        self._item_mappings.pop(group_name, None)

    def add_items(self, group: GroupHandle, item_paths: list[str]) -> dict[str, int]:
        """Add items to an OPC group. Returns mapping of item_path -> item_id."""
        group_name = group.name if hasattr(group, "name") else str(group)
        resp = self._execute({
            "op": "add_items", "group": group_handle_to_ipc(group), "item_paths": item_paths
        })
        mapping = resp["mapping"]

        # Track items for recovery
        current_items = self._group_items.setdefault(group_name, [])
        for p in item_paths:
            if p not in current_items:
                current_items.append(p)
        self._item_mappings.setdefault(group_name, {}).update(mapping)

        return mapping

    def remove_items(self, group: GroupHandle, item_ids: list[int]) -> None:
        """Remove items from an OPC group."""
        group_name = group.name if hasattr(group, "name") else str(group)
        self._execute({
            "op": "remove_items", "group": group_handle_to_ipc(group), "item_ids": item_ids
        })

        # Update tracked items
        mapping = self._item_mappings.get(group_name, {})
        id_to_path = {iid: p for p, iid in mapping.items()}
        for iid in item_ids:
            path = id_to_path.get(iid)
            if path and group_name in self._group_items:
                try:
                    self._group_items[group_name].remove(path)
                except ValueError:
                    pass
            mapping.pop(path, None)

    def read_device(
        self,
        group: GroupHandle,
        item_ids: list[int],
        timeout: float | None = None,
    ) -> list[ItemResult]:
        """Execute synchronous Device read via isolated worker with deadline enforcement.

        If the worker hangs on a blocked COM call, it is immediately terminated
        and reaped, returning TIMEOUT without accumulating latency across retries.
        """
        now_us = int(time.time() * 1_000_000)
        try:
            resp = self._execute(
                {"op": "read_device", "group": group_handle_to_ipc(group), "item_ids": item_ids},
                timeout=timeout,
                allow_retry=False,  # Single-attempt deadline enforcement
            )
            return resp["results"]
        except TimeoutError:
            logger.warning("read_device deadline expired; worker terminated and reaped; returning TIMEOUT")
            return [
                ItemResult(
                    item_id=iid,
                    status=ItemStatus.TIMEOUT,
                    value_type=ValueType.BLOB,
                    quality=0,
                    timestamp_us=now_us,
                    value=b"",
                    error_code=0x80040003,
                )
                for iid in item_ids
            ]
        except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
            logger.error("read_device failed: %s", exc)
            return [
                ItemResult(
                    item_id=iid,
                    status=ItemStatus.ERROR,
                    value_type=ValueType.BLOB,
                    quality=0,
                    timestamp_us=now_us,
                    value=b"",
                    error_code=0x80040001,
                )
                for iid in item_ids
            ]

    def browse_items(self, parent_path: str = "") -> list[BrowseEntry]:
        """Browse items in server namespace."""
        resp = self._execute({"op": "browse_items", "parent_path": parent_path})
        return resp["entries"]

    def get_server_status(self) -> ServerStatus:
        """Query server status."""
        resp = self._execute({"op": "get_server_status"})
        return resp["status"]

    def discover_servers(self) -> list[str]:
        """Discover registered OPC DA servers on local machine."""
        resp = self._execute({"op": "discover_servers"})
        return resp["servers"]

    def simulate_crash(self) -> None:
        """Trigger immediate child process crash (for fault-injection testing)."""
        if self._conn is not None and self.is_alive:
            try:
                self._conn.send({"op": "crash"})
            except Exception:
                logger.debug("Crash injection IPC send failed", exc_info=True)
            time.sleep(0.05)

    def simulate_block(self) -> None:
        """Trigger infinite block in child process to test deadline reaping."""
        if self._conn is not None and self.is_alive:
            try:
                self._conn.send({"op": "block"})
            except Exception:
                logger.debug("Block injection IPC send failed", exc_info=True)
