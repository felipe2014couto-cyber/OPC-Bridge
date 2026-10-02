"""Common definitions and base interfaces for OPC adapters."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from opc_bridge.protocol import ItemResult

# OPC Data Source constants (OPC DA 2.05 standard)
OPC_DS_CACHE = 1
OPC_DS_DEVICE = 2

# OPC Quality bit flags (OPC DA 2.05 standard)
# Quality is a 16-bit field where the high 2 bits of the low byte define Major Quality:
# 00xxxxxx: Bad
# 01xxxxxx: Uncertain
# 11xxxxxx: Good
OPC_QUALITY_BAD = 0x00
OPC_QUALITY_UNCERTAIN = 0x40
OPC_QUALITY_GOOD = 0xC0
OPC_QUALITY_MASK = 0xC0

# OPC Server state constants
OPC_STATUS_RUNNING = "RUNNING"
OPC_STATUS_FAILED = "FAILED"
OPC_STATUS_NOCONFIG = "NO_CONFIG"
OPC_STATUS_SUSPENDED = "SUSPENDED"
OPC_STATUS_TEST = "TEST"


@dataclass
class BrowseEntry:
    """Information about an item or branch in the OPC server namespace."""

    path: str
    name: str
    data_type: str
    access_rights: str
    is_leaf: bool


@dataclass
class ServerStatus:
    """Current operational status of the OPC server."""

    state: str = OPC_STATUS_RUNNING
    vendor_info: str = ""
    version: str = ""
    start_time: float = field(default_factory=time.time)


@dataclass
class GroupHandle:
    """Handle representing a configured OPC group."""

    name: str
    update_rate_ms: int
    native_handle: object = None


def group_handle_to_ipc(handle: GroupHandle) -> dict[str, str | int]:
    """Serialize only the identity of a group, never its native COM handle."""
    return {"name": handle.name, "update_rate_ms": handle.update_rate_ms}


def group_handle_from_ipc(value: dict[str, str | int]) -> GroupHandle:
    """Rebuild a process-local group reference from its primitive IPC form."""
    name = value.get("name")
    update_rate_ms = value.get("update_rate_ms")
    if not isinstance(name, str) or type(update_rate_ms) is not int:
        raise ValueError("Invalid OPC worker group reference")
    return GroupHandle(name=name, update_rate_ms=update_rate_ms)


@runtime_checkable
class OpcAdapter(Protocol):
    """Contract for OPC DA adapters (real COM or simulated)."""

    def connect(self, prog_id: str) -> None: ...
    def disconnect(self) -> None: ...
    def create_group(self, name: str, update_rate_ms: int) -> GroupHandle: ...
    def remove_group(self, handle: GroupHandle) -> None: ...
    def add_items(self, group: GroupHandle, item_paths: list[str]) -> dict[str, int]: ...
    def remove_items(self, group: GroupHandle, item_ids: list[int]) -> None: ...
    def read_device(self, group: GroupHandle, item_ids: list[int]) -> list[ItemResult]: ...
    def browse_items(self, parent_path: str = "") -> list[BrowseEntry]: ...
    def get_server_status(self) -> ServerStatus: ...
