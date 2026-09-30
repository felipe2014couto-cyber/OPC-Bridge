"""Tests for the simulated OPC DA adapter."""
from __future__ import annotations

import struct

import pytest

from opc_bridge.adapters.simulated import (
    BrowseEntry,
    GroupHandle,
    ServerStatus,
    SimulatedOpcAdapter,
)
from opc_bridge.protocol import ItemResult, ItemStatus, ValueType


class TestSimulatedAdapterLifecycle:
    def test_connect_disconnect(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        assert not adapter._connected
        adapter.connect("Simulated.OPC")
        assert adapter._connected
        adapter.disconnect()
        assert not adapter._connected

    def test_double_connect_raises(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        with pytest.raises(RuntimeError, match="Already connected"):
            adapter.connect("Simulated.OPC")

    def test_operations_require_connection(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        with pytest.raises(RuntimeError, match="Not connected"):
            adapter.create_group("g", 1000)


class TestGroupsAndItems:
    def test_create_and_remove_group(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("test-group", 500)
        assert isinstance(group, GroupHandle)
        assert group.name == "test-group"
        assert group.update_rate_ms == 500
        adapter.remove_group(group)
        # Recreating after removal should succeed
        group2 = adapter.create_group("test-group", 500)
        assert group2.name == "test-group"

    def test_duplicate_group_raises(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        adapter.create_group("dup", 100)
        with pytest.raises(ValueError, match="already exists"):
            adapter.create_group("dup", 100)

    def test_add_and_remove_items(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("items-test", 1000)
        mapping = adapter.add_items(group, ["Tag.A", "Tag.B"])
        assert set(mapping.keys()) == {"Tag.A", "Tag.B"}
        assert len(set(mapping.values())) == 2
        item_ids = list(mapping.values())
        adapter.remove_items(group, [item_ids[0]])
        assert item_ids[0] not in group.items
        assert item_ids[1] in group.items


class TestDeviceRead:
    def test_read_returns_results_for_all_requested_items(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("read-test", 1000)
        mapping = adapter.add_items(group, ["Tag.X", "Tag.Y"])
        results = adapter.read_device(group, list(mapping.values()))
        assert len(results) == 2
        for r in results:
            assert isinstance(r, ItemResult)
            assert r.status == ItemStatus.OK
            assert r.quality == 192
            assert r.timestamp_us > 0
            assert r.error_code == 0

    def test_read_missing_item_returns_not_found(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("missing-test", 1000)
        results = adapter.read_device(group, [999])
        assert len(results) == 1
        assert results[0].status == ItemStatus.NOT_FOUND
        assert results[0].error_code != 0

    def test_read_value_type_f64(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("f64-test", 1000)
        mapping = adapter.add_items(group, ["Tag.F64"])
        item_id = mapping["Tag.F64"]
        # Force known base value and zero noise for deterministic check
        group.items[item_id].base_value = 42.0
        group.items[item_id].noise_pct = 0.0
        group.items[item_id].value_type = ValueType.F64
        results = adapter.read_device(group, [item_id])
        assert len(results) == 1
        value = struct.unpack("<d", results[0].value)[0]
        assert value == pytest.approx(42.0)
        assert results[0].value_type == ValueType.F64

    def test_read_with_failure_probability(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("fail-test", 1000)
        mapping = adapter.add_items(group, ["Tag.Fail"])
        item_id = mapping["Tag.Fail"]
        group.items[item_id].fail_probability = 1.0  # always fail
        results = adapter.read_device(group, [item_id])
        assert results[0].status == ItemStatus.ERROR
        assert results[0].error_code != 0


class TestBrowseAndStatus:
    def test_browse_root(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        entries = adapter.browse_items("")
        assert len(entries) == 1
        assert entries[0].path == "Simulated"
        assert not entries[0].is_leaf

    def test_browse_simulated_children(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        entries = adapter.browse_items("Simulated")
        assert len(entries) >= 3
        paths = {e.path for e in entries}
        assert "Simulated.Temperature" in paths
        assert all(isinstance(e, BrowseEntry) for e in entries)

    def test_browse_unknown_path_returns_empty(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        assert adapter.browse_items("NonExistent") == []

    def test_server_status(self):
        adapter = SimulatedOpcAdapter(read_latency_us=0)
        status = adapter.get_server_status()
        assert isinstance(status, ServerStatus)
        assert status.state == "RUNNING"
        assert status.version.startswith("0.1.0")