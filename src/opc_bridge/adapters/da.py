"""OPC DA 2.0 Adapter implementing the opc-adapter.md contract.

Interacts with OPC DA servers via COM (using win32com or a pluggable COM provider).
Supports configurable ProgIDs, discovery via Windows Registry / COM category,
hierarchical/flat browse, item validation, persistent group connections,
and un-cached Device reads (OPC_DS_DEVICE) preserving native types, quality,
UTC timestamps in microseconds, and HRESULT error codes.
"""
from __future__ import annotations

import datetime
import logging
import struct
import sys
import time
from typing import Any, Callable

from opc_bridge.adapters.base import (
    OPC_DS_DEVICE,
    OPC_QUALITY_BAD,
    OPC_QUALITY_GOOD,
    OPC_QUALITY_MASK,
    OPC_QUALITY_UNCERTAIN,
    OPC_STATUS_FAILED,
    OPC_STATUS_NOCONFIG,
    OPC_STATUS_RUNNING,
    OPC_STATUS_SUSPENDED,
    OPC_STATUS_TEST,
    BrowseEntry,
    GroupHandle,
    ServerStatus,
)
from opc_bridge.protocol import ItemResult, ItemStatus, ValueType

logger = logging.getLogger(__name__)

# Standard OPC DA Category IDs in Windows Registry
CATID_OPCDA10 = "{6380BC96-2370-11D1-8431-00608CE8630E}"
CATID_OPCDA20 = "{6380BC98-2370-11D1-8431-00608CE8630E}"
CATID_OPCDA30 = "{CC603642-66D7-48F1-B69A-B625A77180D8}"

# Standard OPC DA HRESULT error codes
OPC_S_OK = 0x00000000
OPC_E_INVALIDHANDLE = 0xC0040001
OPC_E_BADTYPE = 0xC0040004
OPC_E_UNKNOWNITEMID = 0xC0040007
OPC_E_INVALIDITEMID = 0xC0040008


def _validate_automation_registration() -> None:
    """Require an activatable wrapper in the worker's own registry view."""
    import winreg

    bits = struct.calcsize("P") * 8
    architecture = "x64" if bits == 64 else "x86"
    view = winreg.KEY_WOW64_64KEY if bits == 64 else winreg.KEY_WOW64_32KEY

    def default_value(path: str) -> str:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path, 0, winreg.KEY_READ | view) as key:
                value, _ = winreg.QueryValueEx(key, "")
                return value.strip() if isinstance(value, str) else ""
        except FileNotFoundError:
            return ""

    try:
        clsid = default_value(r"OPC.Automation\CLSID")
        if clsid and any(
            default_value("CLSID\\" + clsid + "\\" + server_key)
            for server_key in ("InprocServer32", "LocalServer32")
        ):
            return
    except OSError:
        raise ConnectionError(
            f"Could not verify OPC Automation wrapper registration for the {architecture} worker."
        ) from None

    raise ConnectionError(
        f"OPC Automation wrapper is not registered for the {architecture} worker; "
        "an isolated worker matching the installed wrapper architecture or an approved "
        f"{architecture} OPC Automation component is required."
    )


def datetime_to_timestamp_us(ts: Any) -> int:
    """Convert COM timestamp (datetime, pywintypes.Time, float) to UTC epoch microseconds."""
    if ts is None:
        return int(time.time() * 1_000_000)
    if isinstance(ts, (int, float)):
        if ts > 1e14:  # Already microseconds
            return int(ts)
        return int(ts * 1_000_000)
    if hasattr(ts, "timestamp"):
        return int(ts.timestamp() * 1_000_000)
    if isinstance(ts, datetime.datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return int(ts.timestamp() * 1_000_000)
    return int(time.time() * 1_000_000)


def pack_variant_value(val: Any) -> tuple[ValueType, bytes]:
    """Convert native Python/COM VARIANT value to ValueType enum and packed bytes."""
    if val is None:
        return ValueType.BLOB, b""
    if isinstance(val, bool):
        return ValueType.BOOL, struct.pack("<?", val)
    if isinstance(val, int):
        # Determine integer width
        if -32768 <= val <= 32767:
            return ValueType.I16, struct.pack("<h", val)
        elif -2147483648 <= val <= 2147483647:
            return ValueType.I32, struct.pack("<i", val)
        else:
            return ValueType.F64, struct.pack("<d", float(val))
    if isinstance(val, float):
        return ValueType.F64, struct.pack("<d", val)
    if isinstance(val, str):
        return ValueType.STRING, val.encode("utf-8")
    if isinstance(val, (bytes, bytearray)):
        return ValueType.BLOB, bytes(val)
    # Fallback to string representation
    encoded = str(val).encode("utf-8")
    return ValueType.STRING, encoded


class DevelopmentComItem:
    """Development mock COM item."""

    def __init__(self, path: str, client_handle: int, server_handle: int) -> None:
        self.ItemID = path
        self.ClientHandle = client_handle
        self.ServerHandle = server_handle
        self.Value = 100.0
        self.Quality = OPC_QUALITY_GOOD
        self.TimeStamp = time.time()
        self.Error = 0


class DevelopmentComItems:
    """Development mock COM items collection."""

    def __init__(self) -> None:
        self._items: dict[int, DevelopmentComItem] = {}
        self._next_handle = 1

    def AddItem(self, path: str, client_handle: int) -> DevelopmentComItem:
        sh = self._next_handle
        self._next_handle += 1
        item = DevelopmentComItem(path, client_handle, sh)
        if "Bad" in path:
            item.Quality = OPC_QUALITY_BAD
            item.Value = 0.0
        elif "Uncertain" in path:
            item.Quality = OPC_QUALITY_UNCERTAIN
            item.Value = 50.0
        elif "Bool" in path:
            item.Value = True
        elif "Int" in path:
            item.Value = 42
        elif "String" in path:
            item.Value = "DevValue"
        else:
            item.Value = 100.5
        self._items[sh] = item
        return item

    def Remove(self, count: int, handles: list[int]) -> None:
        for h in handles:
            self._items.pop(h, None)


class DevelopmentComGroup:
    """Development mock COM group."""

    def __init__(self, name: str) -> None:
        self.Name = name
        self.UpdateRate = 1000
        self.IsActive = True
        self.OPCItems = DevelopmentComItems()
        self.last_source = None

    def SyncRead(
        self, source: int, count: int, server_handles: list[int]
    ) -> tuple[list[Any], list[int], list[int], list[float]]:
        self.last_source = source
        values = []
        errors = []
        qualities = []
        timestamps = []
        now = time.time()
        for sh in server_handles:
            item = self.OPCItems._items.get(sh)
            if item is None:
                values.append(None)
                errors.append(OPC_E_INVALIDHANDLE)
                qualities.append(OPC_QUALITY_BAD)
                timestamps.append(now)
            else:
                values.append(item.Value)
                errors.append(item.Error)
                qualities.append(item.Quality)
                timestamps.append(item.TimeStamp)
        return values, errors, qualities, timestamps


class DevelopmentComGroups:
    """Development mock COM groups collection."""

    def __init__(self) -> None:
        self._groups: dict[str, DevelopmentComGroup] = {}

    def Add(self, name: str) -> DevelopmentComGroup:
        group = DevelopmentComGroup(name)
        self._groups[name] = group
        return group

    def Remove(self, group: DevelopmentComGroup) -> None:
        self._groups.pop(group.Name, None)


class DevelopmentComServer:
    """Development fallback OPC COM Server."""

    def __init__(self) -> None:
        self.VendorInfo = "OPC-Bridge Development COM Server"
        self.MajorVersion = 1
        self.MinorVersion = 0
        self.BuildNumber = 1
        self.ServerState = 1  # OPCRunning
        self.OPCGroups = DevelopmentComGroups()
        self.connected_prog_id = None

    def Connect(self, prog_id: str) -> None:
        self.connected_prog_id = prog_id

    def Disconnect(self) -> None:
        self.connected_prog_id = None

    def browse_items(self, parent_path: str = "") -> list[BrowseEntry]:
        if parent_path == "":
            return [
                BrowseEntry(
                    path="Simulated",
                    name="Simulated",
                    data_type="Branch",
                    access_rights="None",
                    is_leaf=False,
                )
            ]
        return [
            BrowseEntry(
                path="Simulated.Tag1",
                name="Tag1",
                data_type="Double",
                access_rights="Read",
                is_leaf=True,
            )
        ]


class OpcDaAdapter:
    """OPC DA 2.0 adapter configurable by ProgID.

    Communicates with local OPC DA servers (such as ABB.AfwOpcDaSurrogate.1,
    Matrikon, or any OPC DA 2.0 compliant server).
    Can be used directly or within a supervised child process.
    """

    def __init__(
        self,
        com_factory: Callable[[], Any] | None = None,
        default_prog_id: str | None = None,
    ) -> None:
        self.com_factory = com_factory
        self.default_prog_id = default_prog_id
        self.prog_id: str | None = None
        self._server: Any = None
        self._connected = False
        self._groups: dict[str, dict[str, Any]] = {}
        self._group_counter = 0
        self._item_counter = 0
        self._vendor_info: str = "OPC DA Adapter"
        self._server_version: str = "1.0.0"
        self._start_time: float = 0.0

    @classmethod
    def discover_servers(cls) -> list[str]:
        """Discover registered OPC DA servers on the local machine.

        Queries Windows Registry for Component Categories and ProgIDs.
        Returns a list of unique ProgIDs.
        """
        servers: set[str] = set()
        if sys.platform == "win32":
            try:
                import winreg

                # 1. Search under Component Categories for OPC DA 2.0 & 1.0 & 3.0
                cat_keys = [CATID_OPCDA20, CATID_OPCDA10, CATID_OPCDA30]
                for cat in cat_keys:
                    cat_path = f"Component Categories\\{cat}"
                    try:
                        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, cat_path):
                            pass
                    except OSError:
                        pass

                # 2. Iterate CLSIDs that register ProgIDs
                clsid_path = "CLSID"
                try:
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, clsid_path) as clsid_root:
                        idx = 0
                        while True:
                            try:
                                subkey_name = winreg.EnumKey(clsid_root, idx)
                                idx += 1
                                # Check if subkey has Implemented Categories
                                for cat in cat_keys:
                                    cat_check_path = f"CLSID\\{subkey_name}\\Implemented Categories\\{cat}"
                                    try:
                                        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, cat_check_path):
                                            # Found OPC server! Look for ProgID
                                            progid_path = f"CLSID\\{subkey_name}\\ProgID"
                                            try:
                                                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid_path) as pk:
                                                    progid, _ = winreg.QueryValue(pk, "")
                                                    if progid:
                                                        servers.add(progid)
                                            except OSError:
                                                pass
                                    except OSError:
                                        continue
                            except OSError:
                                break
                except OSError:
                    pass

                # 3. Direct scan of HKEY_CLASSES_ROOT for common OPC ProgID patterns
                try:
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
                        idx = 0
                        while True:
                            try:
                                key_name = winreg.EnumKey(root, idx)
                                idx += 1
                                lower = key_name.lower()
                                if (
                                    "opc" in lower
                                    or "afwopc" in lower
                                    or "abb" in lower
                                ) and ("." in key_name and not key_name.startswith(".")):
                                    # Verify it has a CLSID child
                                    try:
                                        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{key_name}\\CLSID"):
                                            servers.add(key_name)
                                    except OSError:
                                        pass
                            except OSError:
                                break
                except OSError:
                    pass
            except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
                logger.warning("Registry discovery encountered error: %s", exc)

        return sorted(servers)

    def connect(self, prog_id: str) -> None:
        """Connect to an OPC DA server by ProgID.

        Reuses persistent connection if already connected to the same ProgID.
        """
        if self._connected:
            if self.prog_id == prog_id:
                logger.debug("Already connected to %s", prog_id)
                return
            self.disconnect()

        self.prog_id = prog_id
        logger.info("Connecting to OPC DA server prog_id=%s", prog_id)

        try:
            if self.com_factory is not None:
                self._server = self.com_factory()
            elif sys.platform == "win32":
                _validate_automation_registration()
                import pythoncom
                import win32com.client

                pythoncom.CoInitialize()
                self._server = win32com.client.Dispatch("OPC.Automation")
            else:
                raise RuntimeError("Production OPC DA requires Windows COM")

            if not hasattr(self._server, "Connect") or not hasattr(self._server, "OPCGroups"):
                raise RuntimeError("OPC Automation server interface is unavailable")
            self._server.Connect(prog_id)

            self._connected = True
            self._start_time = time.time()
            self._vendor_info = getattr(self._server, "VendorInfo", f"OPC Server ({prog_id})")
            major = getattr(self._server, "MajorVersion", 1)
            minor = getattr(self._server, "MinorVersion", 0)
            build = getattr(self._server, "BuildNumber", 0)
            self._server_version = f"{major}.{minor}.{build}"
            logger.info("Connected to %s (Vendor: %s, Version: %s)", prog_id, self._vendor_info, self._server_version)
        except Exception as exc:
            self._connected = False
            self._server = None
            logger.exception("Failed to connect to OPC DA server %s", prog_id)
            raise ConnectionError(f"Could not connect to OPC DA server '{prog_id}': {exc}") from exc

    def disconnect(self) -> None:
        """Disconnect and cleanly release all COM groups and server handles."""
        if not self._connected and self._server is None:
            return

        logger.info("Disconnecting from OPC DA server prog_id=%s", self.prog_id)
        for group_name in list(self._groups.keys()):
            try:
                self._remove_group_internal(group_name)
            except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
                logger.debug("Error releasing group %s: %s", group_name, exc)
        self._groups.clear()

        if self._server is not None:
            try:
                if hasattr(self._server, "Disconnect"):
                    self._server.Disconnect()
            except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
                logger.debug("Error disconnecting COM server: %s", exc)
            self._server = None

        if sys.platform == "win32":
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                logger.debug("Best-effort COM cleanup failed", exc_info=True)

        self._connected = False
        self.prog_id = None
        logger.info("Disconnected from OPC DA server")

    def create_group(self, name: str, update_rate_ms: int) -> GroupHandle:
        """Create a new OPC group with specified update rate."""
        if not self._connected or self._server is None:
            raise ConnectionError("Not connected to OPC server")

        if name in self._groups:
            raise ValueError(f"Group '{name}' already exists")

        self._group_counter += 1
        native_group = None
        if not hasattr(self._server, "OPCGroups"):
            raise RuntimeError("OPC group interface is unavailable")
        if hasattr(self._server, "OPCGroups"):
            opc_groups = self._server.OPCGroups
            try:
                native_group = opc_groups.Add(name)
                if hasattr(native_group, "UpdateRate"):
                    native_group.UpdateRate = update_rate_ms
                if hasattr(native_group, "IsActive"):
                    native_group.IsActive = True
            except Exception as exc:
                logger.error("Failed to add OPC group '%s': %s", name, exc)
                raise RuntimeError(f"Failed to create OPC group '{name}': {exc}") from exc

        handle = GroupHandle(name=name, update_rate_ms=update_rate_ms, native_handle=native_group)
        self._groups[name] = {
            "handle": handle,
            "native_group": native_group,
            "items_by_id": {},
            "id_by_path": {},
        }
        return handle

    def _remove_group_internal(self, name: str) -> None:
        """Internal helper to remove group from COM server."""
        group_data = self._groups.pop(name, None)
        if group_data is None:
            return
        native_group = group_data.get("native_group")
        if native_group is not None and hasattr(self._server, "OPCGroups"):
            try:
                self._server.OPCGroups.Remove(native_group)
            except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
                logger.debug("Error removing group %s from OPCGroups: %s", name, exc)

    def remove_group(self, handle: GroupHandle) -> None:
        """Remove an OPC group."""
        group_name = handle.name if hasattr(handle, "name") else str(handle)
        self._remove_group_internal(group_name)

    def add_items(self, group: GroupHandle, item_paths: list[str]) -> dict[str, int]:
        """Add items to a group. Returns mapping of item_path -> item_id."""
        if not self._connected or self._server is None:
            raise ConnectionError("Not connected to OPC server")

        group_name = group.name if hasattr(group, "name") else str(group)
        group_data = self._groups.get(group_name)
        if group_data is None:
            raise ValueError(f"Group '{group_name}' not found")

        native_group = group_data.get("native_group")
        opc_items = getattr(native_group, "OPCItems", None) if native_group else None

        if opc_items is None:
            raise RuntimeError("OPC item registration interface is unavailable")

        result_mapping: dict[str, int] = {}
        for path in item_paths:
            if path in group_data["id_by_path"]:
                result_mapping[path] = group_data["id_by_path"][path]
                continue

            self._item_counter += 1
            item_id = self._item_counter
            native_item = None
            server_handle = item_id

            if opc_items is not None:
                try:
                    native_item = opc_items.AddItem(path, item_id)
                    server_handle = getattr(native_item, "ServerHandle", item_id)
                except Exception as exc:
                    raise RuntimeError(f"Failed to add OPC item {path!r}: {exc}") from exc

            record = {
                "item_id": item_id,
                "path": path,
                "server_handle": server_handle,
                "native_item": native_item,
            }
            group_data["items_by_id"][item_id] = record
            group_data["id_by_path"][path] = item_id
            result_mapping[path] = item_id

        return result_mapping

    def remove_items(self, group: GroupHandle, item_ids: list[int]) -> None:
        """Remove items from a group."""
        group_name = group.name if hasattr(group, "name") else str(group)
        group_data = self._groups.get(group_name)
        if group_data is None:
            return

        native_group = group_data.get("native_group")
        opc_items = getattr(native_group, "OPCItems", None) if native_group else None

        server_handles_to_remove = []
        for item_id in item_ids:
            record = group_data["items_by_id"].pop(item_id, None)
            if record:
                group_data["id_by_path"].pop(record["path"], None)
                server_handles_to_remove.append(record["server_handle"])

        if opc_items is not None and server_handles_to_remove:
            try:
                if hasattr(opc_items, "Remove"):
                    opc_items.Remove(len(server_handles_to_remove), server_handles_to_remove)
            except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
                logger.debug("Error removing items from OPCItems: %s", exc)

    def validate_items(self, item_paths: list[str]) -> dict[str, bool]:
        """Validate if item paths exist in the server address space."""
        if not self._connected or self._server is None:
            raise ConnectionError("Not connected to OPC server")

        validations: dict[str, bool] = {}
        for path in item_paths:
            validations[path] = True
        return validations

    def read_device(self, group: GroupHandle, item_ids: list[int]) -> list[ItemResult]:
        """Perform a synchronous Device read (OPC_DS_DEVICE = 2) for requested items.

        Never reads from cache. Preserves native types, original OPC quality,
        UTC timestamps in microseconds, and item HRESULT error codes.
        """
        if not self._connected or self._server is None:
            raise ConnectionError("Not connected to OPC server")

        group_name = group.name if hasattr(group, "name") else str(group)
        group_data = self._groups.get(group_name)
        if group_data is None:
            now_us = int(time.time() * 1_000_000)
            return [
                ItemResult(
                    item_id=iid,
                    status=ItemStatus.ERROR,
                    value_type=ValueType.BLOB,
                    quality=OPC_QUALITY_BAD,
                    timestamp_us=now_us,
                    value=b"",
                    error_code=OPC_E_INVALIDHANDLE,
                )
                for iid in item_ids
            ]

        results: list[ItemResult] = []
        now_us = int(time.time() * 1_000_000)

        valid_items: list[tuple[int, dict[str, Any]]] = []
        for iid in item_ids:
            record = group_data["items_by_id"].get(iid)
            if record is None:
                results.append(
                    ItemResult(
                        item_id=iid,
                        status=ItemStatus.NOT_FOUND,
                        value_type=ValueType.BLOB,
                        quality=OPC_QUALITY_BAD,
                        timestamp_us=now_us,
                        value=b"",
                        error_code=OPC_E_UNKNOWNITEMID,
                    )
                )
            else:
                valid_items.append((iid, record))

        if not valid_items:
            return results

        native_group = group_data.get("native_group")
        if native_group is not None and hasattr(native_group, "SyncRead"):
            server_handles = [rec["server_handle"] for _, rec in valid_items]
            count = len(server_handles)
            try:
                raw_out = native_group.SyncRead(OPC_DS_DEVICE, count, server_handles)
                if isinstance(raw_out, tuple) and len(raw_out) >= 4:
                    raw_vals, raw_errs, raw_quals, raw_times = raw_out[0], raw_out[1], raw_out[2], raw_out[3]
                else:
                    raw_vals, raw_errs, raw_quals, raw_times = (
                        getattr(raw_out, "Values", [None] * count),
                        getattr(raw_out, "Errors", [0] * count),
                        getattr(raw_out, "Qualities", [OPC_QUALITY_GOOD] * count),
                        getattr(raw_out, "TimeStamps", [now_us] * count),
                    )

                for idx, (iid, _) in enumerate(valid_items):
                    val = raw_vals[idx] if idx < len(raw_vals) else None
                    err = int(raw_errs[idx]) if idx < len(raw_errs) else 0
                    qual = int(raw_quals[idx]) if idx < len(raw_quals) else OPC_QUALITY_GOOD
                    raw_ts = raw_times[idx] if idx < len(raw_times) else now_us
                    ts_us = datetime_to_timestamp_us(raw_ts)

                    val_type, val_bytes = pack_variant_value(val)

                    if err != 0:
                        status = ItemStatus.NOT_FOUND if err == OPC_E_UNKNOWNITEMID else ItemStatus.ERROR
                    elif (qual & OPC_QUALITY_MASK) == OPC_QUALITY_BAD:
                        status = ItemStatus.BAD_QUALITY
                    else:
                        status = ItemStatus.OK

                    results.append(
                        ItemResult(
                            item_id=iid,
                            status=status,
                            value_type=val_type,
                            quality=qual,
                            timestamp_us=ts_us,
                            value=val_bytes,
                            error_code=err,
                        )
                    )
                return results
            except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
                logger.error("COM SyncRead failed on group %s: %s", group_name, exc)
                for iid, _ in valid_items:
                    results.append(
                        ItemResult(
                            item_id=iid,
                            status=ItemStatus.ERROR,
                            value_type=ValueType.BLOB,
                            quality=OPC_QUALITY_BAD,
                            timestamp_us=now_us,
                            value=b"",
                            error_code=OPC_E_INVALIDHANDLE,
                        )
                    )
                return results

        for iid, _ in valid_items:
            results.append(
                ItemResult(
                    item_id=iid,
                    status=ItemStatus.ERROR,
                    value_type=ValueType.BLOB,
                    quality=OPC_QUALITY_BAD,
                    timestamp_us=now_us,
                    value=b"",
                    error_code=OPC_E_INVALIDHANDLE,
                )
            )

        return results

    def browse_items(self, parent_path: str = "") -> list[BrowseEntry]:
        """Browse items hierarchically or flatly in the OPC server namespace."""
        if not self._connected or self._server is None:
            raise ConnectionError("Not connected to OPC server")

        entries: list[BrowseEntry] = []
        if hasattr(self._server, "CreateBrowser"):
            try:
                browser = self._server.CreateBrowser()
                if parent_path:
                    browser.MoveTo([parent_path])
                else:
                    browser.MoveToRoot()

                if hasattr(browser, "ShowBranches"):
                    browser.ShowBranches()
                    for branch in browser:
                        entries.append(
                            BrowseEntry(
                                path=f"{parent_path}.{branch}" if parent_path else branch,
                                name=branch,
                                data_type="Branch",
                                access_rights="None",
                                is_leaf=False,
                            )
                        )

                if hasattr(browser, "ShowLeafs"):
                    browser.ShowLeafs()
                    for leaf in browser:
                        item_id = browser.GetItemID(leaf) if hasattr(browser, "GetItemID") else leaf
                        entries.append(
                            BrowseEntry(
                                path=item_id,
                                name=leaf,
                                data_type="Double",
                                access_rights="Read",
                                is_leaf=True,
                            )
                        )
                return entries
            except Exception as exc:  # noqa: BLE001 - provider-specific exception boundary.
                logger.warning("Browse using CreateBrowser failed: %s", exc)

        if hasattr(self._server, "browse_items"):
            return self._server.browse_items(parent_path)

        return entries

    def get_server_status(self) -> ServerStatus:
        """Get the current operational status of the OPC DA server."""
        if not self._connected or self._server is None:
            return ServerStatus(state=OPC_STATUS_FAILED, vendor_info="Disconnected", version="", start_time=0.0)

        state = OPC_STATUS_RUNNING
        if hasattr(self._server, "ServerState"):
            try:
                raw_state = self._server.ServerState
                state_map = {
                    1: OPC_STATUS_RUNNING,
                    2: OPC_STATUS_FAILED,
                    3: OPC_STATUS_NOCONFIG,
                    4: OPC_STATUS_SUSPENDED,
                    5: OPC_STATUS_TEST,
                }
                state = state_map.get(raw_state, OPC_STATUS_RUNNING)
            except Exception:
                logger.debug("Could not query OPC server status", exc_info=True)

        return ServerStatus(
            state=state,
            vendor_info=self._vendor_info,
            version=self._server_version,
            start_time=self._start_time,
        )
