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
        def raises(expected_exception):
            @contextlib.contextmanager
            def cm():
                try:
                    yield
                except expected_exception:
                    pass
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

    def __init__(self) -> None:
        self._groups: dict[str, MockComGroup] = {}

    def Add(self, name: str) -> MockComGroup:
        group = MockComGroup(name)
        self._groups[name] = group
        return group

    def Remove(self, group: MockComGroup) -> None:
        self._groups.pop(group.Name, None)


class MockComServer:
    """Mock COM OPC Server."""

    def __init__(self) -> None:
        self.VendorInfo = "ABB Industrial Simulation (Mock)"
        self.MajorVersion = 5
        self.MinorVersion = 1
        self.BuildNumber = 104
        self.ServerState = 1  # OPCRunning
        self.OPCGroups = MockComGroups()
        self.connected_prog_id = None
        self.connected = False

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

    def test_service_logging_redacts_secrets(self, tmp_path):
        import asyncio

        from opc_bridge.agent.service import run_agent_main

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
        run_agent_main(cfg, stop_event=stop_event)

        log_content = log_file.read_text(encoding="utf-8")
        assert "super-secret-cleartext-token" not in log_content
        assert "[REDACTED]" in log_content

        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)

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
    print("[PASS] TestOpcDaAdapter (9 unit tests)")

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
    print("ALL 19 OPC DA & SUPERVISION TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)
