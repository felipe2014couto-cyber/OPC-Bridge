"""Comprehensive tests for OPC write operation administration, protocol, kill switch, allowlists, and auditing."""
from __future__ import annotations

import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from opc_bridge.adapters.simulated import SimulatedOpcAdapter
from opc_bridge.agent.write import execute_agent_write
from opc_bridge.protocol.write import (
    WRITES_DISABLED_MESSAGE,
    WriteItemRequest,
    WriteItemResult,
    WriteRequest,
    WriteResponse,
    is_writes_enabled,
    validate_item_value,
)
from opc_bridge.server.admin import create_app
from opc_bridge.server.admin.write_ops import parse_tag_specification
from opc_bridge.server.core import AgentSession, BridgeServer, ServerConfig
from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database
from tests.test_admin_api import ADMIN_TOKEN, request
from tests.test_tag_ui import Writer


def test_kill_switch_defaults_to_false():
    with patch.dict(os.environ, {}, clear=True):
        assert not is_writes_enabled()

    for false_val in ["0", "false", "False", "no", "DISABLED", ""]:
        with patch.dict(os.environ, {"OPC_BRIDGE_ENABLE_WRITES": false_val}):
            assert not is_writes_enabled()

    for true_val in ["1", "true", "True", "yes", "YES"]:
        with patch.dict(os.environ, {"OPC_BRIDGE_ENABLE_WRITES": true_val}):
            assert is_writes_enabled()


def test_parse_tag_specification_defaults_write_enabled_to_false():
    # String tag
    spec_str = parse_tag_specification("LINE1.TEMP")
    assert spec_str is not None
    assert spec_str["opc_item_path"] == "LINE1.TEMP"
    assert spec_str["write_enabled"] is False
    assert spec_str["data_type"] == "float"

    # Dict tag without write_enabled
    spec_dict = parse_tag_specification({
        "opc_item_path": "LINE1.SPEED",
        "data_type": "integer",
        "min_value": 0,
        "max_value": 100,
    })
    assert spec_dict is not None
    assert spec_dict["write_enabled"] is False
    assert spec_dict["min_value"] == 0
    assert spec_dict["max_value"] == 100

    # Explicit write_enabled: True
    spec_writable = parse_tag_specification({
        "opc_item_path": "LINE1.SETPOINT",
        "write_enabled": True,
        "data_type": "float",
        "min_value": 10.0,
        "max_value": 50.0,
    })
    assert spec_writable is not None
    assert spec_writable["write_enabled"] is True


def test_validate_item_value_boolean():
    # Valid booleans
    valid, val, err = validate_item_value(True, "boolean")
    assert valid and val is True

    valid, val, err = validate_item_value("true", "boolean")
    assert valid and val is True

    valid, val, err = validate_item_value("1", "boolean")
    assert valid and val is True

    valid, val, err = validate_item_value("sim", "boolean")
    assert valid and val is True

    valid, val, err = validate_item_value("false", "boolean")
    assert valid and val is False

    valid, val, err = validate_item_value("0", "boolean")
    assert valid and val is False

    # Invalid booleans
    valid, val, err = validate_item_value("invalid", "boolean")
    assert not valid
    assert "boolean" in err


def test_validate_item_value_integer():
    valid, val, err = validate_item_value(42, "integer")
    assert valid and val == 42

    valid, val, err = validate_item_value("100", "integer")
    assert valid and val == 100

    # Floating point or invalid string
    valid, val, err = validate_item_value(42.5, "integer")
    assert not valid

    valid, val, err = validate_item_value("42.5", "integer")
    assert not valid

    valid, val, err = validate_item_value("abc", "integer")
    assert not valid


def test_validate_item_value_float():
    valid, val, err = validate_item_value(123.45, "float")
    assert valid and abs(val - 123.45) < 1e-6

    valid, val, err = validate_item_value("123.45", "float")
    assert valid and abs(val - 123.45) < 1e-6

    valid, val, err = validate_item_value("abc", "float")
    assert not valid


def test_validate_item_value_string():
    valid, val, err = validate_item_value("test_string", "string")
    assert valid and val == "test_string"

    valid, val, err = validate_item_value(123, "string")
    assert valid and val == "123"


def test_validate_item_value_bounds():
    # min and max bounds
    valid, val, err = validate_item_value(50, "integer", min_value=10, max_value=100)
    assert valid

    valid, val, err = validate_item_value(10, "integer", min_value=10, max_value=100)
    assert valid

    valid, val, err = validate_item_value(100, "integer", min_value=10, max_value=100)
    assert valid

    # Below min
    valid, val, err = validate_item_value(9, "integer", min_value=10, max_value=100)
    assert not valid
    assert "minimum" in err

    # Above max
    valid, val, err = validate_item_value(101, "integer", min_value=10, max_value=100)
    assert not valid
    assert "maximum" in err


def test_validate_item_value_allowed_values():
    allowed = ["MANUAL", "AUTO", "REMOTE"]
    valid, val, err = validate_item_value("AUTO", "string", allowed_values=allowed)
    assert valid

    valid, val, err = validate_item_value("OFF", "string", allowed_values=allowed)
    assert not valid
    assert "allowed" in err


def test_agent_write_blocked_when_kill_switch_disabled():
    adapter = SimulatedOpcAdapter()
    with patch.dict(os.environ, {"OPC_BRIDGE_ENABLE_WRITES": "false"}):
        req = WriteRequest(
            request_id="req-1",
            opc_prog_id="Sim.Server",
            items=[WriteItemRequest(tag="TAG1", value=10.0, data_type="float")],
            user="admin",
        )
        resp = execute_agent_write(req, adapter, allowlist={"TAG1": {"write_enabled": True}})
        assert resp.error == WRITES_DISABLED_MESSAGE
        assert len(resp.results) == 1
        assert resp.results[0].status == "rejected"


def test_agent_write_rejects_tag_not_in_allowlist():
    adapter = SimulatedOpcAdapter()
    adapter.connect("Sim.Server")
    with patch.dict(os.environ, {"OPC_BRIDGE_ENABLE_WRITES": "true"}):
        req = WriteRequest(
            request_id="req-2",
            opc_prog_id="Sim.Server",
            items=[
                WriteItemRequest(tag="AUTHORIZED_TAG", value=10.0, data_type="float"),
                WriteItemRequest(tag="UNAUTHORIZED_TAG", value=20.0, data_type="float"),
            ],
            user="admin",
        )
        allowlist = {"AUTHORIZED_TAG": {"write_enabled": True}}
        resp = execute_agent_write(req, adapter, allowlist=allowlist)
        assert resp.error is None
        assert len(resp.results) == 2

        res_auth = next(r for r in resp.results if r.tag == "AUTHORIZED_TAG")
        res_unauth = next(r for r in resp.results if r.tag == "UNAUTHORIZED_TAG")

        assert res_auth.status == "applied"
        assert res_unauth.status == "rejected"
        assert "not authorized" in res_unauth.error


def test_agent_write_executes_successfully_when_enabled():
    adapter = SimulatedOpcAdapter()
    adapter.connect("Sim.Server")
    with patch.dict(os.environ, {"OPC_BRIDGE_ENABLE_WRITES": "true"}):
        req = WriteRequest(
            request_id="req-3",
            opc_prog_id="Sim.Server",
            items=[WriteItemRequest(tag="SP_SPEED", value=1500, data_type="integer")],
            user="admin",
        )
        allowlist = {"SP_SPEED": {"write_enabled": True, "data_type": "integer"}}
        resp = execute_agent_write(req, adapter, allowlist=allowlist)
        assert resp.error is None
        assert len(resp.results) == 1
        assert resp.results[0].status == "applied"


@pytest.fixture
def write_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    database = sqlite_for_tests(str(tmp_path / "write_test.sqlite"))
    upgrade_database(database)

    with database.session() as repo:
        repo.add_agent("agent-w1", "Agent Write 1")
        repo.add_credential("agent-w1", "cred-w1", "SECRET-HASH")
        repo.add_equipment(
            equipment_id="eq-200",
            name="Linha de Laminação 2",
            ip_address="192.168.1.200",
            agent_id="agent-w1",
        )
        tags = [
            {
                "opc_item_path": "LAM2.SPEED_READ",
                "description": "Velocidade Medida",
                "write_enabled": False,  # Read-only
                "data_type": "float",
            },
            {
                "opc_item_path": "LAM2.SPEED_SETPOINT",
                "description": "Setpoint de Velocidade",
                "write_enabled": True,   # Explicitly writable
                "data_type": "float",
                "min_value": 0.0,
                "max_value": 2000.0,
            },
        ]
        repo.add_named_config(
            config_id="cfg-200",
            name="Config Velocidade",
            equipment_id="eq-200",
            opc_prog_id="Sim.ProgID",
            interval_ms=1000,
            tags_json=json.dumps(tags),
            agent_id="agent-w1",
        )

    bridge = BridgeServer(ServerConfig(persistence=database))
    writer = Writer()
    session = AgentSession("connected", writer, "agent-w1", "host", "os", ["opc-write-v1"], config_version=1)
    bridge._sessions[session.session_id] = session

    app = create_app(database, bridge)
    return app, bridge, database, writer, session


def test_get_tags_returns_all_tags_with_write_enabled_status(write_runtime):
    app, _, _, _, _ = write_runtime
    status, _, data = request(app, "/api/v1/write-operation/tags?equipment_id=eq-200", method="GET")
    assert status.startswith("200")
    assert "tags" in data
    assert len(data["tags"]) == 2

    read_tag = next(t for t in data["tags"] if t["opc_item_path"] == "LAM2.SPEED_READ")
    write_tag = next(t for t in data["tags"] if t["opc_item_path"] == "LAM2.SPEED_SETPOINT")

    assert read_tag["write_enabled"] is False
    assert write_tag["write_enabled"] is True
    assert write_tag["min_value"] == 0.0
    assert write_tag["max_value"] == 2000.0


def test_validate_endpoint_checks_bounds_and_write_permission(write_runtime):
    app, _, _, _, _ = write_runtime

    # 1. Test invalid tag (write_enabled=False)
    status, _, data = request(app, "/api/v1/write-operation/validate", method="POST", payload={
        "equipment_id": "eq-200",
        "items": [{"tag": "LAM2.SPEED_READ", "value": 500.0}],
    })
    assert status.startswith("200")
    assert data["valid"] is False
    assert "write_enabled=false" in data["items"][0]["error"]

    # 2. Test out-of-bounds on writable tag
    status, _, data = request(app, "/api/v1/write-operation/validate", method="POST", payload={
        "equipment_id": "eq-200",
        "items": [{"tag": "LAM2.SPEED_SETPOINT", "value": 2500.0}],
    })
    assert status.startswith("200")
    assert data["valid"] is False
    assert "maximum" in data["items"][0]["error"]

    # 3. Test valid value on writable tag
    status, _, data = request(app, "/api/v1/write-operation/validate", method="POST", payload={
        "equipment_id": "eq-200",
        "items": [{"tag": "LAM2.SPEED_SETPOINT", "value": 1200.0}],
    })
    assert status.startswith("200")
    assert data["valid"] is True


def test_execute_blocked_by_kill_switch_by_default(write_runtime):
    app, _, _, _, _ = write_runtime
    with patch.dict(os.environ, {}, clear=True):
        status, _, data = request(app, "/api/v1/write-operation/execute", method="POST", payload={
            "equipment_id": "eq-200",
            "confirmed": True,
            "items": [{"tag": "LAM2.SPEED_SETPOINT", "value": 1000.0}],
        })
        assert status.startswith("403")
        assert data["error"] == "writes_disabled"


def test_execute_requires_explicit_confirmation(write_runtime):
    app, _, _, _, _ = write_runtime
    with patch.dict(os.environ, {"OPC_BRIDGE_ENABLE_WRITES": "true"}):
        status, _, data = request(app, "/api/v1/write-operation/execute", method="POST", payload={
            "equipment_id": "eq-200",
            "confirmed": False,  # Not confirmed!
            "items": [{"tag": "LAM2.SPEED_SETPOINT", "value": 1000.0}],
        })
        assert status.startswith("400")
        assert data["error"] == "confirmation_required"


def test_execute_rejects_read_only_tag(write_runtime):
    app, _, _, _, _ = write_runtime
    with patch.dict(os.environ, {"OPC_BRIDGE_ENABLE_WRITES": "true"}):
        status, _, data = request(app, "/api/v1/write-operation/execute", method="POST", payload={
            "equipment_id": "eq-200",
            "confirmed": True,
            "items": [{"tag": "LAM2.SPEED_READ", "value": 500.0}],
        })
        assert status.startswith("403")
        assert data["error"] == "unauthorized_tag"


def test_execute_and_audit_log_success_flow(write_runtime, monkeypatch):
    app, bridge, database, _, session = write_runtime
    with patch.dict(os.environ, {"OPC_BRIDGE_ENABLE_WRITES": "true"}):
        # Mock bridge live values and write execution
        monkeypatch.setattr(bridge, "get_live_values", lambda agent_id: {
            "items": [{"opc_item_path": "LAM2.SPEED_SETPOINT", "value": 850.0}]
        })

        mock_response = WriteResponse(
            request_id="req-999",
            results=[
                WriteItemResult(tag="LAM2.SPEED_SETPOINT", status="applied", value=1500.0)
            ],
        )
        monkeypatch.setattr(bridge, "write_agent_threadsafe", lambda agent_id, req: mock_response)

        # Execute write
        status, _, data = request(app, "/api/v1/write-operation/execute", method="POST", payload={
            "equipment_id": "eq-200",
            "confirmed": True,
            "username": "operador_felipe",
            "items": [{"tag": "LAM2.SPEED_SETPOINT", "value": 1500.0}],
        })
        assert status.startswith("200")
        assert data["status"] == "completed"
        assert data["results"][0]["status"] == "applied"

        # Check persistent audit events
        status_audit, _, audit_data = request(app, "/api/v1/write-operation/audit?equipment_id=eq-200", method="GET")
        assert status_audit.startswith("200")
        events = audit_data.get("audit_events", [])
        assert len(events) >= 1

        latest = events[0]
        assert latest["agent_id"] == "agent-w1"
        assert latest["detail"]["tag"] == "LAM2.SPEED_SETPOINT"
        assert latest["detail"]["user"] == "operador_felipe"
        assert latest["detail"]["previous_value"] == 850.0
        assert latest["detail"]["requested_value"] == 1500.0
        assert latest["detail"]["status"] == "applied"


def test_ui_contains_operacao_opc_module_and_elements(write_runtime):
    app, _, _, _, _ = write_runtime
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/ui", "HTTP_AUTHORIZATION": f"Bearer {ADMIN_TOKEN}"}
    html = b"".join(app(environ, start_response)).decode("utf-8")
    assert captured["status"].startswith("200")
    assert "tab-write-opc" in html
    assert "panel-write-opc" in html
    assert "write-spreadsheet-body" in html
    assert "modal-write-confirm" in html
    assert "Operação OPC" in html
