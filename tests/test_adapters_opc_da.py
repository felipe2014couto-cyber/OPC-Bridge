"""Unit and integration tests for OpcDaAdapter and SupervisedOpcAdapter.

Tests the opc-adapter.md contract:
- Configurable ProgID (not hardcoded to ABB)
- Discovery, browse, item validation
- Persistent connection and group management
- Un-cached Device read (OPC_DS_DEVICE)
- Quality, timestamp in us UTC, native type, and HRESULT preservation
- Isolated COM worker execution and automatic crash recovery
"""
from __future__ import annotations

import logging
import os
import struct
import sys
import time
from contextlib import nullcontext
from types import ModuleType
from typing import Any

try:
    import pytest
except ImportError:
    import contextlib

    class PytestFallback:
        class _Mark:
            @staticmethod
            def parametrize(*args, **kwargs):
                def decorator(func):
                    return func
                return decorator

        mark = _Mark()

        @staticmethod
        def approx(val, rel=None, tolerance=1e-4):
            class ApproxVal:
                def __init__(self, v):
                    self.v = v
                def __eq__(self, other):
                    import builtins
                    return builtins.abs(self.v - other) < tolerance
            return ApproxVal(val)

        @staticmethod
        def raises(expected_exception, match=None):
            class ExceptionInfo:
                def __init__(self):
                    self.value = None

            @contextlib.contextmanager
            def cm():
                info = ExceptionInfo()
                try:
                    yield info
                except expected_exception as exc:
                    info.value = exc
                    if match is not None:
                        import re
                        if not re.search(match, str(exc)):
                            raise AssertionError(f"Pattern {match!r} does not match {str(exc)!r}") from exc
                else:
                    raise AssertionError(f"Expected exception {expected_exception} was not raised")
            return cm()

        @staticmethod
        def fixture(func):
            return func

    pytest = PytestFallback()

from opc_bridge.adapters.base import (
    OPC_DS_DEVICE,
    OPC_QUALITY_BAD,
    OPC_QUALITY_GOOD,
    OPC_QUALITY_UNCERTAIN,
    BrowseEntry,
)
from opc_bridge.adapters.da import (
    OpcDaAdapter,
    datetime_to_timestamp_us,
    pack_variant_value,
)
from opc_bridge.adapters.supervised import SupervisedOpcAdapter
from opc_bridge.protocol import ItemStatus, ValueType


class MockComItem:
    """Mock OPCItem object representing a registered item in a COM group."""

    def __init__(self, path: str, client_handle: int, server_handle: int) -> None:
        self.ItemID = path
        self.ClientHandle = client_handle
        self.ServerHandle = server_handle
        self.Value = 42.5
        self.Quality = OPC_QUALITY_GOOD
        self.TimeStamp = time.time()
        self.Error = 0


class MockComItems:
    """Mock OPCItems collection."""

    def __init__(self) -> None:
        self._items: dict[int, MockComItem] = {}
        self._next_handle = 100

    def AddItem(self, item_path: str, client_handle: int) -> MockComItem:
        sh = self._next_handle
        self._next_handle += 1
        item = MockComItem(item_path, client_handle, sh)
        # Set simulated values based on path name
        if "BadQuality" in item_path:
            item.Quality = OPC_QUALITY_BAD
            item.Value = 0.0
        elif "Uncertain" in item_path:
            item.Quality = OPC_QUALITY_UNCERTAIN
            item.Value = 99.9
        elif "Integer" in item_path:
            item.Value = 1234
        elif "Boolean" in item_path:
            item.Value = True
        elif "String" in item_path:
            item.Value = "ABB_STATUS_RUNNING"
        elif "Float32" in item_path:
            item.Value = 3.14159
        elif "Error" in item_path:
            item.Error = 0xC0040007  # OPC_E_UNKNOWNITEMID
        else:
            item.Value = 250.75
        self._items[sh] = item
        return item

    def Remove(self, count: int, server_handles: list[int]) -> None:
        for sh in server_handles:
            self._items.pop(sh, None)


class MockComGroup:
    """Mock OPCGroup object."""

    def __init__(self, name: str) -> None:
        self.Name = name
        self.UpdateRate = 1000
        self.IsActive = True
        self.OPCItems = MockComItems()
        self.sync_read_count = 0
        self.last_source = None

    def SyncRead(
        self, source: int, count: int, server_handles: list[int]
    ) -> tuple[list[Any], list[int], list[int], list[float]]:
        """Mock synchronous read. Enforces Device source."""
        self.sync_read_count += 1
        self.last_source = source
        assert source == OPC_DS_DEVICE, f"Expected OPC_DS_DEVICE (2), got {source}"

        values: list[Any] = []
        errors: list[int] = []
        qualities: list[int] = []
        timestamps: list[float] = []

        now = time.time()
        for sh in server_handles:
            item = self.OPCItems._items.get(sh)
            if item is None:
                values.append(None)
                errors.append(0xC0040001)  # OPC_E_INVALIDHANDLE
                qualities.append(OPC_QUALITY_BAD)
                timestamps.append(now)
            else:
                values.append(item.Value)
                errors.append(item.Error)
                qualities.append(item.Quality)
                timestamps.append(item.TimeStamp)

        return values, errors, qualities, timestamps


class MockComGroups:
    """Mock OPCGroups collection."""

    def __init__(self, server: Optional[Any] = None) -> None:
        self._groups: dict[str, Any] = {}
        self._server = server

    def Add(self, name: str) -> Any:
        if self._server is not None and hasattr(self._server, "_create_group"):
            group = self._server._create_group(name)
        else:
            group = MockComGroup(name)
        self._groups[name] = group
        return group

    def Remove(self, group: Any) -> None:
        name = getattr(group, "Name", None)
        if name:
            self._groups.pop(name, None)


class MockComServer:
    """Mock COM OPC Server."""

    def __init__(self) -> None:
        self.VendorInfo = "ABB Industrial Simulation (Mock)"
        self.MajorVersion = 5
        self.MinorVersion = 1
        self.BuildNumber = 104
        self.ServerState = 1  # OPCRunning
        self.OPCGroups = MockComGroups(self)
        self.connected_prog_id = None
        self.connected = False

    def _create_group(self, name: str) -> MockComGroup:
        return MockComGroup(name)

    def Connect(self, prog_id: str) -> None:
        self.connected_prog_id = prog_id
        self.connected = True

    def Disconnect(self) -> None:
        self.connected = False
        self.connected_prog_id = None

    def browse_items(self, parent_path: str = "") -> list[BrowseEntry]:
        if parent_path == "":
            return [
                BrowseEntry(
                    path="Pims_A40",
                    name="Pims_A40",
                    data_type="Branch",
                    access_rights="None",
                    is_leaf=False,
                ),
            ]
        if parent_path == "Pims_A40":
            return [
                BrowseEntry(
                    path="Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
                    name="DIAMETRO_CALC_BOBIN",
                    data_type="Double",
                    access_rights="Read",
                    is_leaf=True,
                ),
                BrowseEntry(
                    path="Pims_A40:gUsw.ToPims.VELOCIDADE_LINHA",
                    name="VELOCIDADE_LINHA",
                    data_type="Double",
                    access_rights="Read",
                    is_leaf=True,
                ),
            ]
        return []


@pytest.fixture
def mock_com_adapter() -> OpcDaAdapter:
    """Create an OpcDaAdapter instance backed by MockComServer."""
    return OpcDaAdapter(com_factory=lambda: MockComServer())


class TestOpcDaAdapter:
    """Unit tests for OpcDaAdapter contract compliance."""

    @pytest.mark.parametrize("registration", ["x64", "x86-only", "absent"])
    def test_connect_activates_automation_wrapper_before_abb(self, monkeypatch, registration):
        """Simulate Windows activation without invoking COM or the ABB server."""
        calls = []

        class RecordingComServer(MockComServer):
            def Connect(self, prog_id):
                calls.append(("Connect", prog_id))
                super().Connect(prog_id)

        server = RecordingComServer()

        winreg = ModuleType("winreg")
        winreg.HKEY_CLASSES_ROOT = "HKCR"
        winreg.KEY_READ = 0x20019
        winreg.KEY_WOW64_64KEY = 0x100
        winreg.KEY_WOW64_32KEY = 0x200
        clsid = "{28E68F9A-8D75-11D1-8DC3-3C302A000000}"
        registry = {}
        if registration != "absent":
            registered_view = 0x100 if registration == "x64" else 0x200
            registry[(registered_view, r"OPC.Automation\CLSID")] = clsid
            registry[(registered_view, "CLSID\\" + clsid + r"\InprocServer32")] = "OPCDAAuto.dll"
            # A ProgID name alone in the x64 view cannot activate a class.
            registry[(0x100, "OPC.Automation")] = "OPC Automation"

        def open_key(root, path, reserved, access):
            assert root == winreg.HKEY_CLASSES_ROOT
            assert reserved == 0
            assert access == winreg.KEY_READ | winreg.KEY_WOW64_64KEY
            key = (access & 0x300, path)
            if key not in registry:
                raise FileNotFoundError()
            return nullcontext(key)

        winreg.OpenKey = open_key
        winreg.QueryValueEx = lambda key, name: (registry[key], 1)
        monkeypatch.setitem(sys.modules, "winreg", winreg)
        from opc_bridge.adapters import da

        original_calcsize = da.struct.calcsize
        monkeypatch.setattr(da.struct, "calcsize", lambda fmt: 8 if fmt == "P" else original_calcsize(fmt))

        def dispatch(prog_id):
            calls.append(("Dispatch", prog_id))
            return server

        pythoncom = ModuleType("pythoncom")
        pythoncom.CoInitialize = lambda: None
        pythoncom.CoUninitialize = lambda: None
        win32com = ModuleType("win32com")
        client = ModuleType("win32com.client")
        client.Dispatch = dispatch
        win32com.client = client
        monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
        monkeypatch.setitem(sys.modules, "win32com", win32com)
        monkeypatch.setitem(sys.modules, "win32com.client", client)
        monkeypatch.setattr(sys, "platform", "win32")

        adapter = OpcDaAdapter()
        if registration != "x64":
            with pytest.raises(ConnectionError, match="not registered for the x64 worker"):
                adapter.connect("ABB.AfwOpcDaSurrogate.1")
            assert calls == []
            assert adapter._server is None
            assert adapter._connected is False
            return
        try:
            adapter.connect("ABB.AfwOpcDaSurrogate.1")
            assert adapter._connected is True
            assert calls == [
                ("Dispatch", "OPC.Automation"),
                ("Connect", "ABB.AfwOpcDaSurrogate.1"),
            ]
        finally:
            adapter.disconnect()

    def test_discovery(self):
        """Discovery enumerates registered servers without crashing."""
        servers = OpcDaAdapter.discover_servers()
        assert isinstance(servers, list)

    def test_connect_and_status(self, mock_com_adapter):
        """Adapter connects to specified ProgID and reports server status."""
        adapter = mock_com_adapter
        prog_id = "ABB.AfwOpcDaSurrogate.1"
        adapter.connect(prog_id)

        assert adapter._connected is True
        assert adapter.prog_id == prog_id

        status = adapter.get_server_status()
        assert status.state == "RUNNING"
        assert "ABB" in status.vendor_info
        assert status.version == "5.1.104"
        assert status.start_time > 0

        adapter.disconnect()
        assert adapter._connected is False

    def test_group_creation_and_removal(self, mock_com_adapter):
        """Groups are created, configured, and removed cleanly."""
        adapter = mock_com_adapter
        adapter.connect("Matrikon.OPC.Simulation.1")

        handle = adapter.create_group("Group1", 500)
        assert handle.name == "Group1"
        assert handle.update_rate_ms == 500
        assert "Group1" in adapter._groups

        # Cannot create duplicate group
        with pytest.raises(ValueError):
            adapter.create_group("Group1", 1000)

        adapter.remove_group(handle)
        assert "Group1" not in adapter._groups
        adapter.disconnect()

    def test_add_and_remove_items(self, mock_com_adapter):
        """Items are added and mapped to stable IDs, and removed cleanly."""
        adapter = mock_com_adapter
        adapter.connect("ABB.AfwOpcDaSurrogate.1")
        group = adapter.create_group("ItemsGroup", 250)

        paths = [
            "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
            "Pims_A40:gUsw.ToPims.VELOCIDADE_LINHA",
        ]
        mapping = adapter.add_items(group, paths)

        assert len(mapping) == 2
        for p in paths:
            assert p in mapping
            assert isinstance(mapping[p], int)

        # Adding same item returns existing ID
        mapping2 = adapter.add_items(group, [paths[0]])
        assert mapping2[paths[0]] == mapping[paths[0]]

        # Remove item
        adapter.remove_items(group, [mapping[paths[0]]])
        adapter.disconnect()

    def test_read_device_enforces_device_source(self, mock_com_adapter):
        """read_device MUST request OPC_DS_DEVICE (2), never CACHE (1)."""
        adapter = mock_com_adapter
        adapter.connect("ABB.AfwOpcDaSurrogate.1")
        group = adapter.create_group("DeviceReadGroup", 100)

        mapping = adapter.add_items(group, ["Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN"])
        item_id = mapping["Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN"]

        results = adapter.read_device(group, [item_id])
        assert len(results) == 1
        res = results[0]

        # Verify MockComGroup observed OPC_DS_DEVICE
        native_group = adapter._groups[group.name]["native_group"]
        assert native_group.last_source == OPC_DS_DEVICE
        assert native_group.sync_read_count == 1

        assert res.item_id == item_id
        assert res.status == ItemStatus.OK
        assert res.value_type == ValueType.F64
        val = struct.unpack("<d", res.value)[0]
        assert pytest.approx(val) == 250.75
        assert res.quality == OPC_QUALITY_GOOD
        assert res.timestamp_us > 0
        assert res.error_code == 0

        adapter.disconnect()

    def test_read_device_preserves_qualities(self, mock_com_adapter):
        """Preserves original quality and maps bad/uncertain qualities."""
        adapter = mock_com_adapter
        adapter.connect("Custom.ProgId.OPC")
        group = adapter.create_group("QualGroup", 100)

        paths = ["Tag.Good", "Tag.BadQuality", "Tag.Uncertain"]
        mapping = adapter.add_items(group, paths)

        results = adapter.read_device(group, [mapping[p] for p in paths])
        assert len(results) == 3

        # Good
        assert results[0].status == ItemStatus.OK
        assert results[0].quality == OPC_QUALITY_GOOD

        # Bad
        assert results[1].status == ItemStatus.BAD_QUALITY
        assert results[1].quality == OPC_QUALITY_BAD

        # Uncertain
        assert results[2].status == ItemStatus.OK  # Uncertain is still usable
        assert results[2].quality == OPC_QUALITY_UNCERTAIN

        adapter.disconnect()

    def test_read_device_preserves_native_types(self, mock_com_adapter):
        """Preserves integer, float, bool, and string native types."""
        adapter = mock_com_adapter
        adapter.connect("TypeTest.OPC")
        group = adapter.create_group("TypeGroup", 100)

        paths = ["Tag.Integer", "Tag.Boolean", "Tag.String"]
        mapping = adapter.add_items(group, paths)
        results = adapter.read_device(group, [mapping[p] for p in paths])

        # Integer
        assert results[0].value_type in (ValueType.I16, ValueType.I32)
        int_val = struct.unpack("<h", results[0].value)[0] if results[0].value_type == ValueType.I16 else struct.unpack("<i", results[0].value)[0]
        assert int_val == 1234

        # Boolean
        assert results[1].value_type == ValueType.BOOL
        bool_val = struct.unpack("<?", results[1].value)[0]
        assert bool_val is True

        # String
        assert results[2].value_type == ValueType.STRING
        assert results[2].value.decode("utf-8") == "ABB_STATUS_RUNNING"

        adapter.disconnect()

    def test_read_device_preserves_error_code(self, mock_com_adapter):
        """Preserves HRESULT when server reports item read error."""
        adapter = mock_com_adapter
        adapter.connect("ErrorTest.OPC")
        group = adapter.create_group("ErrGroup", 100)

        mapping = adapter.add_items(group, ["Tag.Error"])
        results = adapter.read_device(group, [mapping["Tag.Error"]])

        assert len(results) == 1
        assert results[0].status == ItemStatus.NOT_FOUND
        assert results[0].error_code == 0xC0040007  # OPC_E_UNKNOWNITEMID

        adapter.disconnect()

    def test_browse_items(self, mock_com_adapter):
        """Hierarchical browse returns branches and leaves."""
        adapter = mock_com_adapter
        adapter.connect("BrowseTest.OPC")

        root_entries = adapter.browse_items("")
        assert len(root_entries) == 1
        assert root_entries[0].name == "Pims_A40"
        assert root_entries[0].is_leaf is False

        child_entries = adapter.browse_items("Pims_A40")
        assert len(child_entries) == 2
        assert child_entries[0].is_leaf is True
        assert child_entries[0].name == "DIAMETRO_CALC_BOBIN"

        adapter.disconnect()

    def test_read_device_passes_one_based_handles(self):
        """Confirm that SyncRead receives 1-based server handles [0, h1, h2, ...]."""
        recorded_calls = []

        class RecordingGroup(MockComGroup):
            def SyncRead(self, source, count, server_handles):
                recorded_calls.append({
                    "source": source,
                    "count": count,
                    "handles": list(server_handles),
                })
                return super().SyncRead(source, count, server_handles)

        class RecordingServer(MockComServer):
            def _create_group(self, name):
                return RecordingGroup(name)

        adapter = OpcDaAdapter(com_factory=lambda: RecordingServer())
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("OneBasedTestGroup", 1000)
        mapping = adapter.add_items(group, ["Tag.A", "Tag.B"])
        item_ids = [mapping["Tag.A"], mapping["Tag.B"]]

        results = adapter.read_device(group, item_ids)
        assert len(results) == 2
        assert len(recorded_calls) == 1
        call = recorded_calls[0]
        assert call["source"] == OPC_DS_DEVICE
        assert call["count"] == 2
        # Index 0 must be reserved as 0, followed by the actual handles
        assert call["handles"][0] == 0
        assert len(call["handles"]) == 3
        expected_handles = [adapter._groups[group.name]["items_by_id"][iid]["server_handle"] for iid in item_ids]
        assert call["handles"][1:] == expected_handles

        adapter.disconnect()

    def test_read_device_maps_two_tags_from_one_based_arrays(self):
        """Confirm correct tag mapping when SyncRead returns 1-based arrays."""
        now = time.time()

        class OneBasedGroup(MockComGroup):
            def SyncRead(self, source, count, server_handles):
                assert count == 2
                assert server_handles[0] == 0
                # Return strictly 1-based arrays where index 0 is dummy/error
                values = [None, 42.5, 99.0]
                errors = [0xC0040001, 0, 0]
                qualities = [OPC_QUALITY_BAD, OPC_QUALITY_GOOD, OPC_QUALITY_GOOD]
                timestamps = [now, now + 1, now + 2]
                return values, errors, qualities, timestamps

        class OneBasedServer(MockComServer):
            def _create_group(self, name):
                return OneBasedGroup(name)

        adapter = OpcDaAdapter(com_factory=lambda: OneBasedServer())
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("MappingGroup", 1000)
        mapping = adapter.add_items(group, ["LineA.Diameter", "LineA.Weight"])

        results = adapter.read_device(group, [mapping["LineA.Diameter"], mapping["LineA.Weight"]])
        assert len(results) == 2

        # Item 1: LineA.Diameter -> values[1] (42.5)
        res1 = results[0]
        assert res1.item_id == mapping["LineA.Diameter"]
        assert res1.status == ItemStatus.OK
        assert res1.error_code == 0
        assert res1.quality == OPC_QUALITY_GOOD
        assert struct.unpack("<d", res1.value)[0] == pytest.approx(42.5)

        # Item 2: LineA.Weight -> values[2] (99.0)
        res2 = results[1]
        assert res2.item_id == mapping["LineA.Weight"]
        assert res2.status == ItemStatus.OK
        assert res2.error_code == 0
        assert res2.quality == OPC_QUALITY_GOOD
        assert struct.unpack("<d", res2.value)[0] == pytest.approx(99.0)

        adapter.disconnect()

    def test_read_device_preserves_real_hresult_on_syncread_failure(self):
        """SyncRead exceptions must preserve the real COM HRESULT and not mask it as OPC_E_INVALIDHANDLE."""
        class FailingGroup(MockComGroup):
            def __init__(self, name, exc_to_raise):
                super().__init__(name)
                self.exc_to_raise = exc_to_raise

            def SyncRead(self, source, count, server_handles):
                raise self.exc_to_raise

        class CustomComError(Exception):
            def __init__(self, hr, msg):
                super().__init__(msg)
                self.hresult = hr

        # Case 1: Exception with real COM HRESULT (e.g., E_ACCESSDENIED 0x80070005)
        class HResultServer(MockComServer):
            def _create_group(self, name):
                return FailingGroup(name, CustomComError(0x80070005, "Access Denied"))

        adapter = OpcDaAdapter(com_factory=lambda: HResultServer())
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("FailGroup1", 1000)
        mapping = adapter.add_items(group, ["Tag.X", "Tag.Y"])

        results = adapter.read_device(group, list(mapping.values()))
        assert len(results) == 2
        for r in results:
            assert r.status == ItemStatus.ERROR
            assert r.error_code == 0x80070005
            assert r.error_code != 0xC0040001  # Not masked as OPC_E_INVALIDHANDLE
        adapter.disconnect()

        # Case 2: Generic exception without HRESULT (falls back safely to E_FAIL 0x80004005)
        class GenericErrorServer(MockComServer):
            def _create_group(self, name):
                return FailingGroup(name, RuntimeError("Driver communication interrupted"))

        adapter2 = OpcDaAdapter(com_factory=lambda: GenericErrorServer())
        adapter2.connect("Simulated.OPC")
        group2 = adapter2.create_group("FailGroup2", 1000)
        mapping2 = adapter2.add_items(group2, ["Tag.Z"])

        results2 = adapter2.read_device(group2, list(mapping2.values()))
        assert len(results2) == 1
        assert results2[0].status == ItemStatus.ERROR
        assert results2[0].error_code == 0x80004005
        assert results2[0].error_code != 0xC0040001
        adapter2.disconnect()

    def test_add_items_rejects_invalid_server_handle(self):
        """add_items must reject missing or invalid ServerHandle from COM wrapper."""
        class InvalidHandleItem:
            def __init__(self, handle):
                self.ServerHandle = handle

        class InvalidHandleItems:
            def __init__(self, bad_handle):
                self.bad_handle = bad_handle

            def AddItem(self, path, client_handle):
                return InvalidHandleItem(self.bad_handle)

        class InvalidHandleGroup:
            def __init__(self, bad_handle):
                self.OPCItems = InvalidHandleItems(bad_handle)

        for bad in (None, 0, -5, "not_an_int", False, True):
            class BadServer(MockComServer):
                def _create_group(self, name):
                    return InvalidHandleGroup(bad)

            adapter = OpcDaAdapter(com_factory=lambda: BadServer())
            adapter.connect("Simulated.OPC")
            group = adapter.create_group(f"BadGroup_{bad}", 1000)

            with pytest.raises(RuntimeError) as exc_info:
                adapter.add_items(group, ["Tag.BadHandle"])
            assert "invalid ServerHandle" in str(exc_info.value)
            # Ensure no corrupted record was stored
            assert "Tag.BadHandle" not in adapter._groups[group.name]["id_by_path"]
            adapter.disconnect()


class TestSupervisedOpcAdapter:
    """Tests for out-of-process COM supervision and crash recovery."""

    def test_supervised_basic_lifecycle(self):
        """Supervised adapter starts worker, executes commands, and shuts down."""
        adapter = SupervisedOpcAdapter(worker_module="tests.fake_blocking_worker")
        try:
            adapter.connect("Simulated.OPC")
            assert adapter.is_alive

            group = adapter.create_group("SupGroup", 500)
            mapping = adapter.add_items(group, ["Tag.A", "Tag.B"])
            assert len(mapping) == 2

            results = adapter.read_device(group, list(mapping.values()))
            assert len(results) == 2
            assert results[0].status == ItemStatus.OK

            status = adapter.get_server_status()
            assert status.state == "RUNNING"
        finally:
            adapter.disconnect()
            assert not adapter.is_alive

    def test_supervised_crash_recovery(self):
        """Supervised adapter automatically restarts worker and restores state after crash."""
        adapter = SupervisedOpcAdapter(worker_module="tests.fake_blocking_worker")
        try:
            adapter.connect("RecoverTest.OPC")
            assert adapter.is_alive
            initial_pid = adapter._process.pid

            group = adapter.create_group("CrashRecoveryGroup", 250)
            paths = ["Tag.Restore1", "Tag.Restore2"]
            mapping = adapter.add_items(group, paths)
            assert len(mapping) == 2

            # Simulate worker process crash
            adapter.simulate_crash()
            time.sleep(0.1)

            # Next read_device MUST detect crash, auto-recover, and succeed
            results = adapter.read_device(group, list(mapping.values()))
            assert len(results) == 2
            assert results[0].status == ItemStatus.OK

            # Verify a new worker was spawned
            assert adapter.is_alive
            assert adapter._process.pid != initial_pid
        finally:
            adapter.disconnect()

    def test_supervised_blocked_read_timeout_and_process_reaping(self):
        """A blocked COM read times out within deadline bound, kills and reaps child PID,
        and subsequent read starts a fresh child process returning a newly correlated value."""
        os.environ["TEST_HANG_ON_READ"] = "1"
        adapter = SupervisedOpcAdapter(
            worker_module="tests.fake_blocking_worker",
            command_timeout=1.0,
        )
        try:
            adapter.connect("Simulated.OPC")
            assert adapter.is_alive
            initial_pid = adapter.worker_pid
            assert initial_pid is not None

            group = adapter.create_group("HangGroup", 100)
            paths = ["Tag.Normal", "Tag.Faulty"]
            mapping = adapter.add_items(group, paths)
            assert len(mapping) == 2

            block_item_id = mapping["Tag.Faulty"]
            normal_item_id = mapping["Tag.Normal"]

            # 1. First read requests the item while TEST_HANG_ON_READ=1 in fake worker.
            # Enforce 0.5s deadline.
            start_time = time.monotonic()
            results = adapter.read_device(group, [block_item_id], timeout=0.5)
            elapsed = time.monotonic() - start_time

            # Prove supervisor returns within a strict bound (deadline 0.5s, bound < 1.5s)
            assert 0.4 <= elapsed < 1.5, f"Supervisor did not return within bound: elapsed={elapsed}s"
            assert len(results) == 1
            assert results[0].status == ItemStatus.TIMEOUT
            assert results[0].error_code == 0x80040003

            # Clear hang flag so subsequent child worker executes successfully
            os.environ["TEST_HANG_ON_READ"] = "0"

            # 2. Subsequent requested simulated read must start a fresh child,
            # execute successfully, and return a new correlated value.
            start_time2 = time.monotonic()
            results2 = adapter.read_device(group, [normal_item_id], timeout=2.0)
            _elapsed2 = time.monotonic() - start_time2

            assert len(results2) == 1
            assert results2[0].status == ItemStatus.OK
            assert results2[0].item_id == normal_item_id
            assert results2[0].timestamp_us > results[0].timestamp_us

            # Prove child PID changed
            assert adapter.is_alive
            new_pid = adapter.worker_pid
            assert new_pid is not None
            assert new_pid != initial_pid, f"Child PID did not change after hang (still {initial_pid})"
        finally:
            os.environ.pop("TEST_HANG_ON_READ", None)
            adapter.disconnect()


class TestHelpers:
    """Test helper conversions for timestamps and variants."""

    def test_datetime_to_timestamp_us(self):
        now = time.time()
        us = datetime_to_timestamp_us(now)
        assert abs(us - int(now * 1_000_000)) < 1000

    def test_pack_variant_value(self):
        vt, b = pack_variant_value(True)
        assert vt == ValueType.BOOL
        assert b == b"\x01"

        vt, b = pack_variant_value(100)
        assert vt == ValueType.I16
        assert struct.unpack("<h", b)[0] == 100

        vt, b = pack_variant_value(123.456)
        assert vt == ValueType.F64
        assert pytest.approx(struct.unpack("<d", b)[0]) == 123.456

        vt, b = pack_variant_value("TestString")
        assert vt == ValueType.STRING
        assert b == b"TestString"


class TestServiceAndPackaging:
    """Test Windows service helpers and offline packaging generation."""

    def test_load_config_defaults(self, tmp_path):
        from opc_bridge.agent.service import load_config

        cfg = load_config(str(tmp_path / "nonexistent.json"))
        assert cfg["server_host"] == "127.0.0.1"
        assert cfg["opc_prog_id"] == ""
        assert cfg["auth_token"] == ""
        assert cfg["update_rate_ms"] == 1000

    def test_load_config_custom(self, tmp_path):
        import json

        from opc_bridge.agent.service import load_config

        cfg_file = tmp_path / "custom.json"
        cfg_file.write_text(
            json.dumps({"server_host": "192.168.1.50", "opc_prog_id": "Custom.ProgId.1", "auth_token": "my-token"}),
            encoding="utf-8",
        )
        cfg = load_config(str(cfg_file))
        assert cfg["server_host"] == "192.168.1.50"
        assert cfg["opc_prog_id"] == "Custom.ProgId.1"
        assert cfg["auth_token"] == "my-token"

    def test_service_logging_redacts_secrets(self, tmp_path, monkeypatch=None):
        import asyncio

        from opc_bridge.agent.service import run_agent_main
        import opc_bridge.adapters.supervised

        orig_calcsize = struct.calcsize
        orig_select = getattr(opc_bridge.adapters.supervised, "select_worker_runtime", None)
        if monkeypatch is not None:
            monkeypatch.setattr(
                "opc_bridge.adapters.supervised.select_worker_runtime",
                lambda *a, **k: ("x64", sys.executable),
            )
            if orig_calcsize("P") != 8:
                monkeypatch.setattr(struct, "calcsize", lambda fmt: 8 if fmt == "P" else orig_calcsize(fmt))
        else:
            opc_bridge.adapters.supervised.select_worker_runtime = lambda *a, **k: ("x64", sys.executable)
            if orig_calcsize("P") != 8:
                struct.calcsize = lambda fmt: 8 if fmt == "P" else orig_calcsize(fmt)

        log_file = tmp_path / "agent.log"
        cfg = {
            "server_host": "127.0.0.1",
            "server_port": 8443,
            "agent_id": "test-agent",
            "auth_token": "super-secret-cleartext-token",
            "opc_prog_id": "",
            "log_file": str(log_file),
            "log_level": "INFO",
        }

        stop_event = asyncio.Event()
        stop_event.set()
        try:
            run_agent_main(cfg, stop_event=stop_event)
            log_content = log_file.read_text(encoding="utf-8")
            assert "super-secret-cleartext-token" not in log_content
            assert "[REDACTED]" in log_content
        finally:
            for handler in logging.root.handlers[:]:
                handler.close()
                logging.root.removeHandler(handler)
            if monkeypatch is None and orig_select is not None:
                opc_bridge.adapters.supervised.select_worker_runtime = orig_select
            if monkeypatch is None and struct.calcsize is not orig_calcsize:
                struct.calcsize = orig_calcsize

    def test_setup_config_fails_closed_without_token(self, tmp_path):
        import subprocess
        cfg_file = tmp_path / "agent.json"
        cmd = [
            sys.executable,
            "packaging/windows/setup_config.py",
            "--config-file", str(cfg_file),
            "--unattended",
        ]
        clean_env = {k: v for k, v in os.environ.items() if k != "OPC_AUTH_TOKEN"}
        res = subprocess.run(cmd, env=clean_env, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        assert res.returncode != 0
        assert not cfg_file.exists()

    def test_offline_package_build(self, tmp_path):
        sys.path.insert(0, "packaging/windows")
        from build_package import build_package

        out_dir = str(tmp_path / "offline_bundle")
        with pytest.raises(ValueError, match="required"):
            build_package(out_dir, create_zip=False)
        assert not (tmp_path / "offline_bundle").exists()



if __name__ == "__main__":
    import pathlib
    import tempfile
    print("=" * 60)
    print("Running OPC DA Adapter & Supervised Adapter Test Suite")
    print("=" * 60)

    # 1. Helpers
    th = TestHelpers()
    th.test_datetime_to_timestamp_us()
    th.test_pack_variant_value()
    print("[PASS] TestHelpers")

    # 2. OpcDaAdapter unit tests
    tda = TestOpcDaAdapter()
    tda.test_discovery()
    mock_adapter = OpcDaAdapter(com_factory=lambda: MockComServer())
    tda.test_connect_and_status(mock_adapter)
    mock_adapter = OpcDaAdapter(com_factory=lambda: MockComServer())
    tda.test_group_creation_and_removal(mock_adapter)
    mock_adapter = OpcDaAdapter(com_factory=lambda: MockComServer())
    tda.test_add_and_remove_items(mock_adapter)
    mock_adapter = OpcDaAdapter(com_factory=lambda: MockComServer())
    tda.test_read_device_enforces_device_source(mock_adapter)
    mock_adapter = OpcDaAdapter(com_factory=lambda: MockComServer())
    tda.test_read_device_preserves_qualities(mock_adapter)
    mock_adapter = OpcDaAdapter(com_factory=lambda: MockComServer())
    tda.test_read_device_preserves_native_types(mock_adapter)
    mock_adapter = OpcDaAdapter(com_factory=lambda: MockComServer())
    tda.test_read_device_preserves_error_code(mock_adapter)
    mock_adapter = OpcDaAdapter(com_factory=lambda: MockComServer())
    tda.test_browse_items(mock_adapter)
    tda.test_read_device_passes_one_based_handles()
    tda.test_read_device_maps_two_tags_from_one_based_arrays()
    tda.test_read_device_preserves_real_hresult_on_syncread_failure()
    tda.test_add_items_rejects_invalid_server_handle()
    print("[PASS] TestOpcDaAdapter (13 unit tests)")

    # 3. SupervisedOpcAdapter tests (lifecycle, crash recovery, blocked call timeout + reaping)
    tsup = TestSupervisedOpcAdapter()
    tsup.test_supervised_basic_lifecycle()
    print("[PASS] TestSupervisedOpcAdapter: test_supervised_basic_lifecycle")

    tsup.test_supervised_crash_recovery()
    print("[PASS] TestSupervisedOpcAdapter: test_supervised_crash_recovery")

    tsup.test_supervised_blocked_read_timeout_and_process_reaping()
    print("[PASS] TestSupervisedOpcAdapter: test_supervised_blocked_read_timeout_and_process_reaping")

    # 4. Service and packaging tests
    with tempfile.TemporaryDirectory() as td:
        tmp_p = pathlib.Path(td)
        tsp = TestServiceAndPackaging()
        tsp.test_load_config_defaults(tmp_p)
        tsp.test_load_config_custom(tmp_p)
        tsp.test_service_logging_redacts_secrets(tmp_p)
        tsp.test_setup_config_fails_closed_without_token(tmp_p)
        tsp.test_offline_package_build(tmp_p)
    print("[PASS] TestServiceAndPackaging")

    print("=" * 60)
    print("ALL 23 OPC DA & SUPERVISION TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)
