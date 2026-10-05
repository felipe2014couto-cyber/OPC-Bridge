"""Tests for Equipment and OPC management UI, persistence, validation, and security hygiene."""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import socket
import struct
import time
import uuid

import pytest

import ipaddress
from opc_bridge.adapters.da import DevelopmentComItems
from opc_bridge.adapters.simulated import SimulatedOpcAdapter
from opc_bridge.agent.inspection import inspect_opc
from opc_bridge.protocol import (
    ItemRef,
    ItemResult,
    ItemStatus,
    MsgType,
    ReadResponsePayload,
    ValueType,
)
from opc_bridge.protocol.inspection import CAPABILITY
from opc_bridge.server.admin import create_app
from opc_bridge.server.core import AgentSession, BridgeServer, ServerConfig
from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database
from tests.test_admin_api import ADMIN_TOKEN, request
from tests.test_tag_ui import ROOT_DIR, SerializedInspectionAdapter, Writer

PROG_ID = "ABB.AfwOpcDaSurrogate.1"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    database = sqlite_for_tests(str(tmp_path / "equipment_opc.sqlite"))
    upgrade_database(database)

    with database.session() as repo:
        repo.add_agent("agent-a", "Agent A")
        repo.add_credential("agent-a", "credential-a", "SECRET-HASH")
        repo.add_snapshot(
            "agent-a",
            "active",
            1,
            json.dumps({
                "config_version": 1,
                "update_rate_ms": 5000,
                "opc_prog_id": PROG_ID,
                "items": [{"item_id": 1, "opc_item_path": "Active.Tag", "requested_source": 0}],
                "auth_token": "PRIVATE-TOKEN",
                "certificate": "PRIVATE-CERT",
                "credential_hash": "SECRET-HASH",
            }),
        )
        repo.add_operation("agent-a", "active-op", "active")
        repo.complete_operation("active-op", "applied")

        # Offline agent for disconnected status testing
        repo.add_agent("agent-offline", "Agent Offline")
        repo.add_credential("agent-offline", "credential-offline", "SECRET-HASH-OFFLINE")

    bridge = BridgeServer(ServerConfig(persistence=database))
    with database.session() as repo:
        repo.add_session("agent-a", "connected", "simulated-host", "simulated-OS", 1)

    writer = Writer()
    session = AgentSession("connected", writer, "agent-a", "host", "os", [CAPABILITY], config_version=1)
    bridge._sessions[session.session_id] = session

    app = create_app(database, bridge)
    created_adapters = []
    active = SimulatedOpcAdapter()
    active.connect("Simulated.OPC")

    def factory():
        adapter = SerializedInspectionAdapter()
        created_adapters.append(adapter)
        return adapter

    def inspect(agent_id, inspection):
        return inspect_opc(inspection, factory, active)

    monkeypatch.setattr(bridge, "inspect_agent_threadsafe", inspect)

    return app, bridge, database, writer, session


def asset(app, path: str, token: str | None = None) -> tuple[str, list, str]:
    headers = {"PATH_INFO": path, "REQUEST_METHOD": "GET"}
    if token is not None:
        headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    captured = {}

    def start_response(status, resp_headers):
        captured["status"] = status
        captured["headers"] = resp_headers

    chunks = app(headers, start_response)
    body = b"".join(chunks).decode("utf-8")
    return captured.get("status", "500"), captured.get("headers", []), body


# -----------------------------------------------------------------------------
# 1. CRUD de equipamentos
# -----------------------------------------------------------------------------
def test_equipment_crud(runtime):
    app, _, database, _, _ = runtime

    # Create equipment
    payload = {
        "name": "Forno A",
        "ip_address": "192.168.10.50",
        "agent_id": "agent-a",
    }
    status, _, data = request(app, "/api/v1/equipments", method="POST", payload=payload)
    assert status.startswith("201")
    eq = data["equipment"]
    assert eq["name"] == "Forno A"
    assert eq["ip_address"] == "192.168.10.50"
    assert eq["agent_id"] == "agent-a"
    eq_id = eq["equipment_id"]
    assert eq_id

    # List equipments
    status, _, data = request(app, "/api/v1/equipments")
    assert status.startswith("200")
    items = data["equipments"]
    assert len(items) == 1
    assert items[0]["equipment_id"] == eq_id
    assert items[0]["agent_status"] == "connected"

    # Get single equipment
    status, _, data = request(app, f"/api/v1/equipments/{eq_id}")
    assert status.startswith("200")
    assert data["equipment"]["name"] == "Forno A"

    # Update equipment
    update_payload = {
        "name": "Forno A - Linha 1",
        "ip_address": "192.168.10.51",
        "agent_id": None,
    }
    status, _, data = request(app, f"/api/v1/equipments/{eq_id}", method="PUT", payload=update_payload)
    assert status.startswith("200")
    assert data["equipment"]["name"] == "Forno A - Linha 1"
    assert data["equipment"]["ip_address"] == "192.168.10.51"
    assert data["equipment"]["agent_id"] is None

    # Delete equipment
    status, _, data = request(app, f"/api/v1/equipments/{eq_id}", method="DELETE")
    assert status.startswith("200")
    assert data["deleted"] is True

    # Confirm deletion
    status, _, _ = request(app, f"/api/v1/equipments/{eq_id}")
    assert status.startswith("404")


# -----------------------------------------------------------------------------
# 2. Validação de IP (IPv4 e IPv6) e natureza administrativa
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "valid_ip",
    [
        "192.168.1.1",
        "10.247.168.43",
        "127.0.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "::1",
        "2001:db8::1",
        "fe80::1",
        "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
    ],
)
def test_equipment_ip_validation_valid(runtime, valid_ip):
    app, _, _, _, _ = runtime
    status, _, data = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": f"Machine-{valid_ip[:6]}", "ip_address": valid_ip},
    )
    assert status.startswith("201")
    assert data["equipment"]["ip_address"] == str(ipaddress.ip_address(valid_ip))


@pytest.mark.parametrize(
    "invalid_ip",
    [
        "999.999.999.999",
        "256.0.0.1",
        "192.168.1",
        "invalid-hostname",
        "10.247.168.43/24",
        "fe80:::1",
        "",
        "   ",
        "1.2.3.4.5",
        "http://192.168.1.1",
    ],
)
def test_equipment_ip_validation_invalid(runtime, invalid_ip):
    app, _, _, _, _ = runtime
    status, _, data = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": "Bad Machine", "ip_address": invalid_ip},
    )
    assert status.startswith("400")
    assert data["error"] == "invalid_ip"


def test_equipment_registration_is_purely_administrative_no_network_connection(runtime, monkeypatch):
    """Confirm equipment creation does NOT attempt socket, DCOM, SSH or network probe."""
    app, _, _, _, _ = runtime

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("Network connection must NOT be initiated during equipment registration!")

    monkeypatch.setattr(socket.socket, "connect", forbidden_connect)

    status, _, data = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": "Dryer 3", "ip_address": "10.247.99.123"},
    )
    assert status.startswith("201")


# -----------------------------------------------------------------------------
# 3. Vínculo equipamento/agente e status de conexão
# -----------------------------------------------------------------------------
def test_equipment_agent_status_projection(runtime):
    app, _, _, _, _ = runtime

    # Unassociated
    status, _, data1 = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": "Standalone Eq", "ip_address": "10.0.0.1"},
    )
    assert status.startswith("201")

    # Associated with connected agent-a
    status, _, data2 = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": "Connected Eq", "ip_address": "10.0.0.2", "agent_id": "agent-a"},
    )
    assert status.startswith("201")

    # Associated with disconnected agent-offline
    status, _, data3 = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": "Offline Eq", "ip_address": "10.0.0.3", "agent_id": "agent-offline"},
    )
    assert status.startswith("201")

    # Verify listing projections
    status, _, data = request(app, "/api/v1/equipments")
    assert status.startswith("200")
    by_name = {e["name"]: e for e in data["equipments"]}

    assert by_name["Standalone Eq"]["agent_status"] == "unassociated"
    assert by_name["Connected Eq"]["agent_status"] == "connected"
    assert by_name["Offline Eq"]["agent_status"] == "disconnected"


# -----------------------------------------------------------------------------
# 4. Proteção de exclusão com configurações vinculadas
# -----------------------------------------------------------------------------
def test_equipment_deletion_protection_with_linked_configs(runtime):
    app, _, _, _, _ = runtime

    # Create equipment
    status, _, data = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": "Critical Machine", "ip_address": "10.0.0.10", "agent_id": "agent-a"},
    )
    eq_id = data["equipment"]["equipment_id"]

    # Create named config linked to this equipment
    cfg_payload = {
        "name": "Diâmetro calculado — Linha A",
        "equipment_id": eq_id,
        "opc_prog_id": PROG_ID,
        "interval_ms": 2000,
        "tags": ["Device.Diameter1", "Device.Diameter2"],
    }
    status, _, cfg_data = request(app, "/api/v1/opc-configs", method="POST", payload=cfg_payload)
    assert status.startswith("201")
    cfg_id = cfg_data["config"]["config_id"]

    # Attempt to delete equipment WITHOUT confirmation -> must be rejected 409 Conflict
    status, _, data = request(app, f"/api/v1/equipments/{eq_id}", method="DELETE")
    assert status.startswith("409")
    assert data["error"] == "equipment_has_linked_configs"
    assert len(data["configs"]) == 1
    assert data["configs"][0]["config_id"] == cfg_id
    assert data["configs"][0]["name"] == "Diâmetro calculado — Linha A"

    # Verify equipment and config still exist
    status, _, _ = request(app, f"/api/v1/equipments/{eq_id}")
    assert status.startswith("200")
    status, _, _ = request(app, f"/api/v1/opc-configs/{cfg_id}")
    assert status.startswith("200")

    # Delete WITH explicit confirmation query parameter ?confirmed=true
    status, _, data = request(app, f"/api/v1/equipments/{eq_id}?confirmed=true", method="DELETE")
    assert status.startswith("200")
    assert data["deleted"] is True

    # Equipment and linked config are now deleted
    status, _, _ = request(app, f"/api/v1/equipments/{eq_id}")
    assert status.startswith("404")
    status, _, _ = request(app, f"/api/v1/opc-configs/{cfg_id}")
    assert status.startswith("404")


# -----------------------------------------------------------------------------
# 5. CRUD de configurações OPC nomeadas e alias de rota
# -----------------------------------------------------------------------------
def test_named_opc_config_crud_and_route_alias(runtime):
    app, _, _, _, _ = runtime

    # Create equipment
    status, _, eq_data = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": "Calandra B", "ip_address": "10.0.0.20", "agent_id": "agent-a"},
    )
    eq_id = eq_data["equipment"]["equipment_id"]

    # Create config via /api/v1/opc-configs
    payload = {
        "name": "Velocidade e Tensão",
        "equipment_id": eq_id,
        "opc_prog_id": PROG_ID,
        "interval_ms": 1500,
        "tags": ["Calandra.Speed", "Calandra.Tension"],
    }
    status, _, data = request(app, "/api/v1/opc-configs", method="POST", payload=payload)
    assert status.startswith("201")
    cfg = data["config"]
    assert cfg["name"] == "Velocidade e Tensão"
    assert cfg["equipment_id"] == eq_id
    assert cfg["interval_ms"] == 1500
    assert cfg["tags"] == ["Calandra.Speed", "Calandra.Tension"]
    assert cfg["agent_id"] == "agent-a"
    cfg_id = cfg["config_id"]

    # List configs by equipment
    status, _, data = request(app, f"/api/v1/opc-configs?equipment_id={eq_id}")
    assert status.startswith("200")
    assert len(data["configs"]) == 1
    assert data["configs"][0]["config_id"] == cfg_id

    # Test route alias /api/v1/named-opc-configs
    status, _, data = request(app, f"/api/v1/named-opc-configs/{cfg_id}")
    assert status.startswith("200")
    assert data["config"]["name"] == "Velocidade e Tensão"

    # Update config
    update_payload = {
        "name": "Velocidade, Tensão e Temperatura",
        "equipment_id": eq_id,
        "opc_prog_id": PROG_ID,
        "interval_ms": 3000,
        "tags": ["Calandra.Speed", "Calandra.Tension", "Calandra.Temp"],
    }
    status, _, data = request(app, f"/api/v1/opc-configs/{cfg_id}", method="PUT", payload=update_payload)
    assert status.startswith("200")
    assert data["config"]["name"] == "Velocidade, Tensão e Temperatura"
    assert data["config"]["interval_ms"] == 3000
    assert len(data["config"]["tags"]) == 3

    # Delete config
    status, _, data = request(app, f"/api/v1/opc-configs/{cfg_id}", method="DELETE")
    assert status.startswith("200")
    assert data["deleted"] is True

    # Verify deleted
    status, _, _ = request(app, f"/api/v1/opc-configs/{cfg_id}")
    assert status.startswith("404")


# -----------------------------------------------------------------------------
# 6. Limite de 50 tags
# -----------------------------------------------------------------------------
def test_named_opc_config_tag_limits(runtime):
    app, _, _, _, _ = runtime
    status, _, eq_data = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": "Limits Machine", "ip_address": "10.0.0.30"},
    )
    eq_id = eq_data["equipment"]["equipment_id"]

    # Exactly 50 tags: valid
    fifty_tags = [f"Device.Tag_{i:02d}" for i in range(1, 51)]
    status, _, _ = request(
        app,
        "/api/v1/opc-configs",
        method="POST",
        payload={"name": "50 Tags Plan", "equipment_id": eq_id, "opc_prog_id": PROG_ID, "interval_ms": 1000, "tags": fifty_tags},
    )
    assert status.startswith("201")

    # 51 tags: rejected
    fifty_one_tags = [f"Device.Tag_{i:02d}" for i in range(1, 52)]
    status, _, data = request(
        app,
        "/api/v1/opc-configs",
        method="POST",
        payload={"name": "51 Tags Plan", "equipment_id": eq_id, "opc_prog_id": PROG_ID, "interval_ms": 1000, "tags": fifty_one_tags},
    )
    assert status.startswith("400")
    assert data["error"] == "invalid_tags"

    # 0 tags: rejected
    status, _, data = request(
        app,
        "/api/v1/opc-configs",
        method="POST",
        payload={"name": "Empty Tags Plan", "equipment_id": eq_id, "opc_prog_id": PROG_ID, "interval_ms": 1000, "tags": []},
    )
    assert status.startswith("400")
    assert data["error"] == "invalid_tags"

    # Duplicate tags: rejected
    status, _, data = request(
        app,
        "/api/v1/opc-configs",
        method="POST",
        payload={"name": "Duplicates Plan", "equipment_id": eq_id, "opc_prog_id": PROG_ID, "interval_ms": 1000, "tags": ["Tag.A", "Tag.A"]},
    )
    assert status.startswith("400")
    assert data["error"] == "invalid_tags"


# -----------------------------------------------------------------------------
# 7. Intervalo de 1000–60000 ms
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("valid_interval", [1000, 5000, 30000, 60000])
def test_named_opc_config_valid_interval(runtime, valid_interval):
    app, _, _, _, _ = runtime
    status, _, eq_data = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": f"Eq-{valid_interval}", "ip_address": "10.0.0.40"},
    )
    eq_id = eq_data["equipment"]["equipment_id"]

    status, _, _ = request(
        app,
        "/api/v1/opc-configs",
        method="POST",
        payload={"name": f"Config-{valid_interval}", "equipment_id": eq_id, "opc_prog_id": PROG_ID, "interval_ms": valid_interval, "tags": ["Tag.X"]},
    )
    assert status.startswith("201")


@pytest.mark.parametrize("invalid_interval", [999, 0, -1000, 60001, 100000, "5000"])
def test_named_opc_config_invalid_interval(runtime, invalid_interval):
    app, _, _, _, _ = runtime
    status, _, eq_data = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": "Interval Test Eq", "ip_address": "10.0.0.41"},
    )
    eq_id = eq_data["equipment"]["equipment_id"]

    status, _, data = request(
        app,
        "/api/v1/opc-configs",
        method="POST",
        payload={"name": "Bad Interval", "equipment_id": eq_id, "opc_prog_id": PROG_ID, "interval_ms": invalid_interval, "tags": ["Tag.X"]},
    )
    assert status.startswith("400")
    assert data["error"] == "invalid_interval"


# -----------------------------------------------------------------------------
# 8. Descoberta isolada de servidores OPC
# -----------------------------------------------------------------------------
def test_isolated_opc_servers_discovery(runtime):
    app, _, _, writer, _ = runtime
    initial_frames = len(writer.frames)

    # Remote server discovery using capability
    status, _, data = request(app, "/api/v1/agents/agent-a/opc-servers")
    assert status.startswith("200")
    assert "servers" in data
    assert PROG_ID in data["servers"]

    # Verifies discovery does NOT alter active config or send CONFIG_PUSH
    assert len(writer.frames) == initial_frames


# -----------------------------------------------------------------------------
# 9. Valores ao vivo: qualidade, erro, idade e formato "Último timestamp"
# -----------------------------------------------------------------------------
def test_live_values_fields_and_timestamp_format(runtime):
    app, bridge, _, _, session = runtime

    test_time_sec = 1759665600.123  # Known point in time
    test_time_us = int(test_time_sec * 1_000_000)

    session.config_version = 1
    session.config_items = [
        ItemRef(item_id=1, opc_item_path="Sensor.Temperature"),
        ItemRef(item_id=2, opc_item_path="Sensor.Pressure"),
        ItemRef(item_id=3, opc_item_path="Sensor.ErrorTag"),
    ]

    resp = ReadResponsePayload(
        request_id=201,
        duration_us=1500,
        results=[
            ItemResult(
                item_id=1,
                status=ItemStatus.OK,
                value_type=ValueType.F64,
                quality=192,  # Good
                timestamp_us=test_time_us,
                value=struct.pack("<d", 85.75),
                error_code=0,
            ),
            ItemResult(
                item_id=2,
                status=ItemStatus.OK,
                value_type=ValueType.I32,
                quality=0,  # Bad
                timestamp_us=test_time_us,
                value=struct.pack("<i", 42),
                error_code=0,
            ),
            ItemResult(
                item_id=3,
                status=ItemStatus.ERROR,
                value_type=ValueType.STRING,
                quality=0,
                timestamp_us=test_time_us,
                value=b"",
                error_code=0xC0040007,
            ),
        ],
    )
    bridge._record_live_values(session, resp)

    status, _, data = request(app, "/api/v1/agents/agent-a/live-values")
    assert status.startswith("200")
    assert data["connected"] is True
    items = {it["opc_item_path"]: it for it in data["items"]}

    # Timestamp format dd/MM/yyyy HH:mm:ss.SSS verification
    ts_regex = re.compile(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}\.\d{3}$")

    # Item 1: Good
    t = items["Sensor.Temperature"]
    assert t["status"] == "ok"
    assert t["value"] == 85.75
    assert t["quality"] == 192
    assert t["quality_text"] == "Good"
    assert t["stale"] is False
    assert t["age_ms"] is not None and t["age_ms"] >= 0
    assert ts_regex.match(t["opc_timestamp"]), f"Bad timestamp format: {t['opc_timestamp']}"

    # Item 2: Bad Quality
    p = items["Sensor.Pressure"]
    assert p["quality"] == 0
    assert p["quality_text"] == "Bad"

    # Item 3: Error
    e = items["Sensor.ErrorTag"]
    assert e["status"] == "error"
    assert e["value"] is None
    assert "OPC_E_UNKNOWNITEMID" in e["error"]


# -----------------------------------------------------------------------------
# 10. Expiração e limpeza de valores efêmeros
# -----------------------------------------------------------------------------
def test_live_values_expiration_and_disconnect_cleanup(runtime, monkeypatch):
    app, bridge, _, _, session = runtime
    session.config_version = 1
    session.update_rate_ms = 2000  # TTL = max(3*2000, 15000) = 15000 ms
    session.config_items = [ItemRef(item_id=1, opc_item_path="Sensor.TTL")]

    now_wall = time.time()
    now_mono = time.monotonic()
    resp = ReadResponsePayload(
        request_id=202,
        duration_us=500,
        results=[
            ItemResult(
                item_id=1,
                status=ItemStatus.OK,
                value_type=ValueType.I32,
                quality=192,
                timestamp_us=int(now_wall * 1_000_000),
                value=struct.pack("<i", 100),
                error_code=0,
            ),
        ],
    )
    bridge._record_live_values(session, resp)

    # Fresh
    status, _, data = request(app, "/api/v1/agents/agent-a/live-values")
    assert data["items"][0]["stale"] is False

    # Simulate 20 seconds later (past 15s TTL)
    monkeypatch.setattr(time, "monotonic", lambda: now_mono + 20.0)
    status, _, data = request(app, "/api/v1/agents/agent-a/live-values")
    assert data["items"][0]["stale"] is True
    assert data["items"][0]["status"] == "stale"
    assert data["items"][0]["age_ms"] >= 15000

    # Disconnect cleanup
    bridge.clear_live_values("agent-a")
    del bridge._sessions["connected"]

    status, _, data = request(app, "/api/v1/agents/agent-a/live-values")
    assert data["connected"] is False
    assert data["status"] == "agent_disconnected"
    assert data["items"] == []
    assert "agent-a" not in bridge._live_values


# -----------------------------------------------------------------------------
# 11. Salvar definição não envia CONFIG_PUSH; Aplicar exige confirmação explícita
# -----------------------------------------------------------------------------
def test_saving_definition_does_not_send_config_push(runtime):
    app, _, database, writer, _ = runtime
    initial_frames = len(writer.frames)

    status, _, eq_data = request(
        app,
        "/api/v1/equipments",
        method="POST",
        payload={"name": "Save Test Eq", "ip_address": "10.0.0.50", "agent_id": "agent-a"},
    )
    eq_id = eq_data["equipment"]["equipment_id"]

    # Save named config
    status, _, _ = request(
        app,
        "/api/v1/opc-configs",
        method="POST",
        payload={
            "name": "Saved Only Definition",
            "equipment_id": eq_id,
            "opc_prog_id": PROG_ID,
            "interval_ms": 2500,
            "tags": ["Dev.T1", "Dev.T2"],
        },
    )
    assert status.startswith("201")

    # Confirm 0 frames sent
    assert len(writer.frames) == initial_frames

    # Active config snapshot remains version 1 (intact)
    with database.session() as repo:
        snap = repo.latest_applied_snapshot("agent-a")
        assert snap is not None
        assert snap.version == 1
        active_payload = json.loads(snap.payload_json)
        assert active_payload["items"][0]["opc_item_path"] == "Active.Tag"


def test_apply_to_agent_requires_explicit_confirmation(runtime, monkeypatch):
    app, bridge, _, _, _ = runtime
    dispatched = []
    monkeypatch.setattr(
        bridge,
        "dispatch_admin_config_operation_threadsafe",
        lambda agent, operation, payload, **kwargs: dispatched.append(payload),
    )

    # 1. Validate list to obtain ticket
    val_status, _, val_data = request(
        app,
        "/api/v1/agents/agent-a/tag-validations",
        method="POST",
        payload={"opc_prog_id": PROG_ID, "update_rate_ms": 2000, "tags": ["Active.Tag"]},
    )
    assert val_status.startswith("200")
    ticket = val_data["validation_id"]

    # 2. Applying WITHOUT confirmed=true must fail 400 with confirmation_required
    status, _, data = request(
        app,
        "/api/v1/agents/agent-a/tag-config-operations",
        method="POST",
        payload={
            "opc_prog_id": PROG_ID,
            "update_rate_ms": 2000,
            "tags": ["Active.Tag"],
            "validation_id": ticket,
            "confirmed": False,
        },
    )
    assert status.startswith("400")
    assert data["error"] == "confirmation_required"
    assert len(dispatched) == 0

    # 3. Applying WITH confirmed=true succeeds and dispatches CONFIG_PUSH
    status, _, data = request(
        app,
        "/api/v1/agents/agent-a/tag-config-operations",
        method="POST",
        payload={
            "opc_prog_id": PROG_ID,
            "update_rate_ms": 2000,
            "tags": ["Active.Tag"],
            "validation_id": ticket,
            "confirmed": True,
        },
    )
    assert status.startswith("201")
    assert data["status"] == "pending"
    assert len(dispatched) == 1
    push_payload = dispatched[0]
    assert push_payload.opc_prog_id == PROG_ID
    assert push_payload.update_rate_ms == 2000


# -----------------------------------------------------------------------------
# 12. Ausência estrita de persistência de valores de processo e escrita OPC/PI
# -----------------------------------------------------------------------------
def test_strictly_read_only_and_no_process_values_persisted(runtime, caplog):
    app, bridge, database, _, session = runtime
    secret_process_val = 123456789

    session.config_version = 1
    session.config_items = [ItemRef(item_id=1, opc_item_path="Plant.SecretReactorTemp")]

    resp = ReadResponsePayload(
        request_id=203,
        duration_us=500,
        results=[
            ItemResult(
                item_id=1,
                status=ItemStatus.OK,
                value_type=ValueType.I32,
                quality=192,
                timestamp_us=int(time.time() * 1_000_000),
                value=struct.pack("<i", secret_process_val),
                error_code=0,
            ),
        ],
    )

    caplog.clear()
    with caplog.at_level("DEBUG"):
        bridge._record_live_values(session, resp)
        status, _, data = request(app, "/api/v1/agents/agent-a/live-values")

    assert status.startswith("200")
    assert data["items"][0]["value"] == secret_process_val

    # Check ALL SQLite tables: absolutely NO process values stored
    with database.session() as repo:
        for table in ["audit_events", "config_snapshots", "config_operations", "equipments", "named_opc_configs"]:
            rows = repo._execute(f"SELECT * FROM {table}").fetchall()
            for r in rows:
                row_str = str(r)
                assert str(secret_process_val) not in row_str
                assert "Plant.SecretReactorTemp" not in row_str

    # Check logs: NO process values logged
    for record in caplog.records:
        assert str(secret_process_val) not in record.getMessage()

    # Protocol MsgType verification: strictly NO write messages
    assert all("WRITE" not in msg.name for msg in MsgType)

    # API endpoints: NO write endpoints
    status, _, _ = request(app, "/api/v1/agents/agent-a/write", method="POST", payload={"val": 1})
    assert status.startswith(("404", "405"))


# -----------------------------------------------------------------------------
# 13. Template Nginx sem ACL por IP, HTTPS e Basic Auth obrigatórios
# -----------------------------------------------------------------------------
def test_nginx_template_security_and_user_docs():
    nginx_conf = ROOT_DIR / "deploy" / "nginx" / "opc-bridge-admin-ui.conf.example"
    assert nginx_conf.is_file()
    content = nginx_conf.read_text(encoding="utf-8")

    # Fixed IP ACL must be absent
    assert "allow 10.247.87.39;" not in content

    # HTTPS and Basic Auth mandatory
    assert "listen 10.247.168.43:8081 ssl;" in content
    assert "auth_basic " in content
    assert "auth_basic_user_file /etc/opc-bridge/nginx-admin.htpasswd;" in content

    # Documentation for adding users individually
    assert "sudo htpasswd -B /etc/opc-bridge/nginx-admin.htpasswd NOME_DO_USUARIO" in content

    # External token injection (no token hardcoded in template)
    assert "include /etc/opc-bridge/nginx-api-token.conf;" in content
    assert "<VALOR_DO_ADMIN_API_TOKEN" in content or "ADMIN_API_TOKEN" not in content


# -----------------------------------------------------------------------------
# 14. Frontend hygiene: sem senhas, token, localStorage ou sessionStorage
# -----------------------------------------------------------------------------
def test_frontend_security_hygiene_and_bearer_protection(runtime):
    app, _, _, _, _ = runtime

    status, _, html = asset(app, "/ui", ADMIN_TOKEN)
    assert status.startswith("200")
    # Tabs and panels exist
    assert 'id="tab-equipments"' in html
    assert 'id="tab-opc"' in html
    assert 'id="panel-equipments"' in html
    assert 'id="panel-opc"' in html
    assert "Equipamentos" in html
    assert "OPC" in html
    # Columns exist
    assert "Último timestamp" in html
    assert "Idade" in html
    # No credentials in HTML
    assert "ADMIN_API_TOKEN" not in html
    assert 'type="password"' not in html

    status, _, js = asset(app, "/ui/app.js", ADMIN_TOKEN)
    assert status.startswith("200")
    assert "localStorage" not in js
    assert "sessionStorage" not in js
    assert "ADMIN_API_TOKEN" not in js
    assert "Authorization" not in js

    # Direct API requires Bearer token
    status, _, _ = asset(app, "/api/v1/equipments")
    assert status.startswith("401")
    status, _, _ = asset(app, "/api/v1/opc-configs")
    assert status.startswith("401")
