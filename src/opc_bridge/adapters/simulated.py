"""Simulated OPC DA adapter for development and testing without real hardware.

Follows the contract defined in docs/contracts/opc-adapter.md but uses
in-memory state instead of COM calls. Useful for:
- Agent communication development (T5)
- Server scheduling validation (T3 follow-up)
- Integration tests without Windows/ABB dependency

NOT a replacement for real ABB validation (T4/T7).
"""
from __future__ import annotations

import random
import struct
import threading
import time
from dataclasses import dataclass, field

from opc_bridge.protocol import ItemResult, ItemStatus, ValueType


@dataclass
class SimulatedItem:
    """A simulated OPC item with configurable behavior."""

    path: str
    value_type: ValueType = ValueType.F64
    base_value: float = 100.0
    noise_pct: float = 0.01  # 1% noise by default
    fail_probability: float = 0.0  # 0% failure by default
    quality: int = 192  # Good quality by default


@dataclass
class GroupHandle:
    """Opaque handle for a simulated OPC group."""

    name: str
    update_rate_ms: int
    items: dict[int, SimulatedItem] = field(default_factory=dict)
    _next_item_id: int = 1


@dataclass
class BrowseEntry:
    """Simulated browse result."""

    path: str
    name: str
    data_type: str
    access_rights: str
    is_leaf: bool


@dataclass
class ServerStatus:
    """Simulated server status."""

    state: str = "RUNNING"
    vendor_info: str = "OPC-Bridge Simulated Adapter"
    version: str = "0.1.0-sim"
    start_time: float = field(default_factory=time.time)


class SimulatedOpcAdapter:
    """In-memory OPC DA adapter following the opc-adapter.md contract.

    This adapter simulates Device reads (never cache) with configurable
    latency, noise and failure rates for realistic testing.
    """

    def __init__(self, read_latency_us: int = 50) -> None:
        self._connected = False
        self._prog_id: str | None = None
        self._groups: dict[str, GroupHandle] = {}
        self._read_latency_us = read_latency_us
        self._server_status = ServerStatus()
        self._lock = threading.RLock()

    @property
    def prog_id(self) -> str | None:
        """Return configured ProgID."""
        return self._prog_id

    @property
    def is_connected(self) -> bool:
        """Return whether adapter is connected."""
        return self._connected

    def connect(self, prog_id: str) -> None:
        """Simulate connection to an OPC server."""
        with self._lock:
            if self._connected:
                raise RuntimeError("Already connected")
            self._connected = True
            self._prog_id = prog_id
            self._server_status.start_time = time.time()

    def disconnect(self) -> None:
        """Disconnect and release all resources."""
        with self._lock:
            self._groups.clear()
            self._connected = False
            self._prog_id = None

    def create_group(self, name: str, update_rate_ms: int) -> GroupHandle:
        """Create a new OPC group."""
        with self._lock:
            if not self._connected:
                raise RuntimeError("Not connected")
            if name in self._groups:
                raise ValueError(f"Group '{name}' already exists")
            group = GroupHandle(name=name, update_rate_ms=update_rate_ms)
            self._groups[name] = group
            return group

    def remove_group(self, handle: GroupHandle) -> None:
        """Remove an OPC group."""
        with self._lock:
            name = handle.name if hasattr(handle, "name") else str(handle)
            if name in self._groups:
                del self._groups[name]

    def add_items(
        self, group: GroupHandle, item_paths: list[str]
    ) -> dict[str, int]:
        """Add items to a group. Returns mapping of path -> item_id."""
        with self._lock:
            result: dict[str, int] = {}
            for path in item_paths:
                item_id = group._next_item_id
                group._next_item_id += 1
                group.items[item_id] = SimulatedItem(path=path)
                result[path] = item_id
            return result

    def remove_items(self, group: GroupHandle, item_ids: list[int]) -> None:
        """Remove items from a group."""
        with self._lock:
            for item_id in item_ids:
                group.items.pop(item_id, None)

    def read_device(
        self, group: GroupHandle, item_ids: list[int]
    ) -> list[ItemResult]:
        """Perform a Device read (simulated). Never uses cache.

        Simulates read latency and optional failures per the item config.
        """
        with self._lock:
            if not self._connected:
                raise RuntimeError("Not connected")

        # Simulate read latency
        if self._read_latency_us > 0:
            time.sleep(self._read_latency_us / 1_000_000)

        results: list[ItemResult] = []
        now_us = int(time.time() * 1_000_000)

        for item_id in item_ids:
            item = group.items.get(item_id)
            if item is None:
                results.append(
                    ItemResult(
                        item_id=item_id,
                        status=ItemStatus.NOT_FOUND,
                        value_type=ValueType.I32,
                        quality=0,
                        timestamp_us=now_us,
                        value=b"",
                        error_code=0x80040001,
                    )
                )
                continue

            # Simulate random failures
            if item.fail_probability > 0 and random.random() < item.fail_probability:
                results.append(
                    ItemResult(
                        item_id=item_id,
                        status=ItemStatus.ERROR,
                        value_type=item.value_type,
                        quality=0,
                        timestamp_us=now_us,
                        value=b"",
                        error_code=0x80040002,
                    )
                )
                continue

            # Generate simulated value with noise
            value_bytes = self._generate_value(item)
            results.append(
                ItemResult(
                    item_id=item_id,
                    status=ItemStatus.OK,
                    value_type=item.value_type,
                    quality=item.quality,
                    timestamp_us=now_us,
                    value=value_bytes,
                    error_code=0,
                )
            )

        return results

    def browse_items(self, parent_path: str = "") -> list[BrowseEntry]:
        """Browse available items (simulated hierarchy)."""
        with self._lock:
            if parent_path == "":
                return [
                    BrowseEntry(
                        path="Simulated",
                        name="Simulated",
                        data_type="Folder",
                        access_rights="Read",
                        is_leaf=False,
                    ),
                ]
            if parent_path == "Simulated":
                return [
                    BrowseEntry(
                        path="Simulated.Temperature",
                        name="Temperature",
                        data_type="Double",
                        access_rights="Read",
                        is_leaf=True,
                    ),
                    BrowseEntry(
                        path="Simulated.Pressure",
                        name="Pressure",
                        data_type="Double",
                        access_rights="Read",
                        is_leaf=True,
                    ),
                    BrowseEntry(
                        path="Simulated.Status",
                        name="Status",
                        data_type="String",
                        access_rights="Read",
                        is_leaf=True,
                    ),
                ]
            return []

    def get_server_status(self) -> ServerStatus:
        """Get simulated server status."""
        with self._lock:
            return self._server_status

    def _generate_value(self, item: SimulatedItem) -> bytes:
        """Generate a simulated value with optional noise."""
        noise = random.uniform(-item.noise_pct, item.noise_pct)
        value = item.base_value * (1 + noise)

        if item.value_type == ValueType.F64:
            return struct.pack("<d", value)
        elif item.value_type == ValueType.F32:
            return struct.pack("<f", float(value))
        elif item.value_type == ValueType.I32:
            return struct.pack("<i", int(value))
        elif item.value_type == ValueType.I16:
            return struct.pack("<h", int(value))
        elif item.value_type == ValueType.BOOL:
            return struct.pack("<?", value > 0)
        elif item.value_type == ValueType.STRING:
            return f"{value:.2f}".encode()
        else:
            return struct.pack("<d", value)