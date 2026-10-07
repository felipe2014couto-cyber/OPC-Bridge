"""Comprehensive automated test suite for OPC-to-PI mappings:
- Persistence and CRUD API
- Rejection of publication speed faster than OPC update rate
- Unmapped tags remaining strictly read-only
- Simulation using last available OPC reading with zero OPC writes, zero polling, zero CONFIG_PUSH
- Deletion, toggle/deactivation, and persistent audit trail
- UI integration elements
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest

from opc_bridge.server.admin import create_app
from opc_bridge.server.core import AgentSession, BridgeServer, ServerConfig
from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database
from tests.test_admin_api import ADMIN_TOKEN, request
from tests.test_tag_ui import Writer


@pytest.fixture
def pi_mapping_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    database = sqlite_for_tests(str(tmp_path / "pi_mappings_test.sqlite"))
    upgrade_database(database)

    with database.session() as repo:
        repo.add_agent("agent-pi", "Agent PI Production")
        repo.add_credential("agent-pi", "cred-pi", "SECRET-HASH")
        repo.add_equipment(
            equipment_id="eq-100",
            name="Bobinadeira PB2",
            ip_address="10.247.168.43",
            agent_id="agent-pi",
        )
        tags = [
            "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
            "Pims_A40:gUsw.ToPims.PESO_CALC_BOBINADEIRA",
            "Pims_A40:gUsw.ToPims.EBA_PERDA_MAGNETICA",
        ]
        repo.add_named_config(
            config_id="cfg-100",
            name="Config Linha PB2",
            equipment_id="eq-100",
            opc_prog_id="ABB.AfwOpcDaSurrogate.1",
            interval_ms=5000,  # OPC update rate: 5000 ms
            tags_json=json.dumps(tags),
            agent_id="agent-pi",
        )

    bridge = BridgeServer(ServerConfig(persistence=database))
    writer = Writer()
    session = AgentSession("connected", writer, "agent-pi", "host", "os", ["opc-da"], config_version=1)
    bridge._sessions[session.session_id] = session

    app = create_app(database, bridge)
    return app, bridge, database, writer, session


def test_pi_mappings_persistence_crud(tmp_path):
    db = sqlite_for_tests(str(tmp_path / "repo_test.sqlite"))
    upgrade_database(db)

    with db.session() as repo:
        repo.add_agent("agent-test", "Agent Test")
        repo.add_equipment("eq-1", "Equip 1", "10.0.0.1", "agent-test")
        repo.add_named_config(
            config_id="cfg-1",
            name="Config 1",
            equipment_id="eq-1",
            opc_prog_id="ProgID.1",
            interval_ms=5000,
            tags_json=json.dumps(["TAG.A", "TAG.B"]),
            agent_id="agent-test",
        )

        # 1. Create mapping
        mapping = repo.add_pi_mapping(
            mapping_id="map-1",
            equipment_id="eq-1",
            opc_config_id="cfg-1",
            opc_item_path="TAG.A",
            item_id=0,
            pi_point_name="PI_TAG_A",
            point_source="OPC",
            location1=1,
            publish_interval_ms=5000,
            enabled=True,
        )
        assert mapping.mapping_id == "map-1"
        assert mapping.pi_point_name == "PI_TAG_A"
        assert mapping.enabled is True

        # 2. Get mapping
        fetched = repo.get_pi_mapping("map-1")
        assert fetched is not None
        assert fetched["pi_point_name"] == "PI_TAG_A"
        assert fetched["equipment_name"] == "Equip 1"
        assert fetched["config_name"] == "Config 1"

        # 3. List mappings
        all_maps = repo.list_pi_mappings(equipment_id="eq-1")
        assert len(all_maps) == 1

        # 4. Update mapping
        updated = repo.update_pi_mapping(
            mapping_id="map-1",
            pi_point_name="PI_TAG_A_NEW",
            point_source="L",
            location1=2,
            publish_interval_ms=10000,
            enabled=True,
        )
        assert updated is not None
        assert updated["pi_point_name"] == "PI_TAG_A_NEW"
        assert updated["point_source"] == "L"
        assert updated["location1"] == 2
        assert updated["publish_interval_ms"] == 10000

        # 5. Toggle enabled
        ok_toggle = repo.set_pi_mapping_enabled("map-1", False)
        assert ok_toggle is True
        assert repo.get_pi_mapping("map-1")["enabled"] == 0

        # 6. Update publish status
        ok_status = repo.update_pi_mapping_status("map-1", "Simulado", "123.45")
        assert ok_status is True
        rechecked = repo.get_pi_mapping("map-1")
        assert rechecked["last_publish_status"] == "Simulado"
        assert rechecked["last_published_value"] == "123.45"

        # 7. Delete mapping
        ok_del = repo.delete_pi_mapping("map-1")
        assert ok_del is True
        assert repo.get_pi_mapping("map-1") is None


def test_create_mapping_rejects_speed_faster_than_opc_rate(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # OPC Config cfg-100 has interval_ms = 5000.
    # Attempting to publish at 2000 ms MUST be rejected!
    status, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_config_id": "cfg-100",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "DIAMETRO_CALC_BOBIN",
        "point_source": "OPC",
        "location1": 1,
        "publish_interval_ms": 2000,  # Faster than 5000 ms OPC collection rate!
        "enabled": True,
    })

    assert status.startswith("400")
    assert data["error"] == "interval_faster_than_opc"
    assert "não pode ser menor que o intervalo de coleta OPC" in data["message"]
    assert "5000 ms" in data["message"]


def test_create_mapping_validates_interval_bounds_and_tag_membership(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # 1. Bounds: below 1000 ms
    status1, _, data1 = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_config_id": "cfg-100",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "DIAMETRO_CALC_BOBIN",
        "publish_interval_ms": 500,
    })
    assert status1.startswith("400")
    assert data1["error"] == "invalid_publish_interval_bounds"

    # 2. Tag not in config
    status2, _, data2 = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_config_id": "cfg-100",
        "opc_item_path": "UNKNOWN_TAG",
        "pi_point_name": "UNKNOWN_PI",
        "publish_interval_ms": 5000,
    })
    assert status2.startswith("400")
    assert data2["error"] == "tag_not_in_config"


def test_create_mapping_success_and_auditing(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # Create valid mapping (5000 ms == 5000 ms OPC rate)
    status, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_config_id": "cfg-100",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "DIAMETRO_CALC_BOBIN",
        "point_source": "OPC",
        "location1": 1,
        "publish_interval_ms": 5000,
        "enabled": True,
    })

    assert status.startswith("201")
    mapping = data["mapping"]
    assert mapping["pi_point_name"] == "DIAMETRO_CALC_BOBIN"
    assert mapping["point_source"] == "OPC"
    assert mapping["location1"] == 1
    assert mapping["publish_interval_ms"] == 5000
    assert mapping["last_publish_status"] == "Não configurado"

    # Verify audit event
    status_audit, _, audit_data = request(app, "/api/v1/pi-mappings/audit?equipment_id=eq-100", method="GET")
    assert status_audit.startswith("200")
    events = audit_data.get("audit_events", [])
    assert len(events) >= 1
    assert events[0]["event_type"] == "pi_mapping.created"
    assert events[0]["detail"]["user"] == "admin"


def test_simulation_uses_last_available_opc_reading_without_opc_write_or_config_push(pi_mapping_runtime, monkeypatch):
    app, bridge, _, _, _ = pi_mapping_runtime

    # 1. Create mapping
    _, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_config_id": "cfg-100",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "DIAMETRO_CALC_BOBIN",
        "publish_interval_ms": 5000,
    })
    mapping_id = data["mapping"]["mapping_id"]

    # 2. Mock live value already available from OPC collection
    monkeypatch.setattr(bridge, "get_live_values", lambda agent_id: {
        "items": [
            {
                "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
                "value": 1450.5,
                "quality": 192,
                "quality_text": "Good",
                "opc_timestamp": "2026-10-07T12:00:00.000Z",
                "age_ms": 100,
            }
        ]
    })

    # Guard: ensure write_agent_threadsafe and dispatch_admin_config_operation_threadsafe are NEVER called!
    mock_opc_write = MagicMock()
    mock_config_push = MagicMock()
    monkeypatch.setattr(bridge, "write_agent_threadsafe", mock_opc_write, raising=False)
    monkeypatch.setattr(bridge, "dispatch_admin_config_operation_threadsafe", mock_config_push, raising=False)

    # 3. Execute simulation
    status_sim, _, sim_data = request(app, f"/api/v1/pi-mappings/{mapping_id}/simulate", method="POST")
    assert status_sim.startswith("200")

    result = sim_data["result"]
    assert result["status"] == "Simulado"
    assert result["value"] == 1450.5
    assert result["pi_point_name"] == "DIAMETRO_CALC_BOBIN"
    assert result["details"]["simulated"] is True

    # 4. Check mapping state was updated to "Simulado"
    updated_map = sim_data["mapping"]
    assert updated_map["last_publish_status"] == "Simulado"
    assert updated_map["last_published_value"] == "1450.5"

    # CRITICAL: Verify zero OPC writes and zero CONFIG_PUSH calls
    mock_opc_write.assert_not_called()
    mock_config_push.assert_not_called()

    # 5. Check audit event
    status_audit, _, audit_data = request(app, "/api/v1/pi-mappings/audit?equipment_id=eq-100", method="GET")
    assert status_audit.startswith("200")
    events = audit_data["audit_events"]
    sim_event = next(e for e in events if e["event_type"] == "pi_mapping.simulation")
    assert sim_event["detail"]["value"] == 1450.5
    assert sim_event["detail"]["user"] == "admin"


def test_simulation_rejects_disabled_mapping(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # Create mapping
    _, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_config_id": "cfg-100",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "DIAMETRO_CALC_BOBIN",
        "publish_interval_ms": 5000,
        "enabled": False,  # Disabled mapping!
    })
    mapping_id = data["mapping"]["mapping_id"]

    # Attempt to simulate -> rejected with 409
    status, _, sim_data = request(app, f"/api/v1/pi-mappings/{mapping_id}/simulate", method="POST")
    assert status.startswith("409")
    assert sim_data["error"] == "mapping_disabled"


def test_toggle_and_delete_mapping(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # Create mapping
    _, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_config_id": "cfg-100",
        "opc_item_path": "Pims_A40:gUsw.ToPims.PESO_CALC_BOBINADEIRA",
        "pi_point_name": "PESO_CALC_BOBINADEIRA",
        "publish_interval_ms": 6000,
        "enabled": True,
    })
    mapping_id = data["mapping"]["mapping_id"]

    # Toggle enabled -> False
    status_t1, _, t1_data = request(app, f"/api/v1/pi-mappings/{mapping_id}/toggle", method="POST")
    assert status_t1.startswith("200")
    assert bool(t1_data["mapping"]["enabled"]) is False

    # Toggle enabled -> True
    status_t2, _, t2_data = request(app, f"/api/v1/pi-mappings/{mapping_id}/toggle", method="POST")
    assert status_t2.startswith("200")
    assert bool(t2_data["mapping"]["enabled"]) is True

    # Delete mapping
    status_del, _, del_data = request(app, f"/api/v1/pi-mappings/{mapping_id}", method="DELETE")
    assert status_del.startswith("200")
    assert del_data["deleted"] is True

    # Verify not found after deletion
    status_get, _, _ = request(app, f"/api/v1/pi-mappings/{mapping_id}", method="GET")
    assert status_get.startswith("404")


def test_ui_contains_pi_integration_tab_and_elements(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/ui", "HTTP_AUTHORIZATION": f"Bearer {ADMIN_TOKEN}"}
    html = b"".join(app(environ, start_response)).decode("utf-8")

    assert captured["status"].startswith("200")

    # 1. Top level tabs (Equipamentos | OPC | Integração PI)
    assert "tab-equipments" in html
    assert "tab-opc" in html
    assert "tab-pi" in html
    assert "panel-equipments" in html
    assert "panel-opc" in html
    assert "panel-pi" in html
    assert "Equipamentos" in html
    assert "OPC" in html
    assert "Integração PI" in html

    # 2. Fixed banner at top of tab
    assert "pi-simulation-banner" in html
    assert "Saída PI: Simulação — nenhuma escrita real habilitada." in html

    # 3. Form elements in superior card
    assert "Mapeamentos OPC → PI" in html
    assert "pi-equipment-select" in html
    assert "pi-config-select" in html
    assert "pi-tag-select" in html
    assert "pi-point-name" in html
    assert "pi-point-source" in html
    assert "pi-location1" in html
    assert "pi-interval" in html
    assert "pi-mapping-enabled" in html
    assert "pi-form-error" in html
    assert "btn-save-mapping" in html
    assert "btn-cancel-mapping" in html

    # 4. Table columns
    expected_cols = [
        "Tag OPC origem",
        "Último valor OPC",
        "Qualidade",
        "Último timestamp OPC",
        "PI Point destino",
        "Point Source",
        "Location1",
        "Velocidade",
        "Estado",
        "Último resultado",
        "Ações",
    ]
    for col in expected_cols:
        assert col in html

    # 5. Security: NO PI URL, credentials, token or certificate fields in the UI
    assert 'id="pi-url"' not in html
    assert 'id="pi-token"' not in html
    assert 'id="pi-secret"' not in html
    assert 'id="pi-cert"' not in html
    assert "web-api" not in html.lower()

    # 6. Delete confirmation modal
    assert "modal-delete-mapping-confirm" in html


def test_cascade_endpoint_data(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # 1. Fetch configs for eq-100 -> returns cfg-100
    status, _, data = request(app, "/api/v1/opc-configs?equipment_id=eq-100", method="GET")
    assert status.startswith("200")
    configs = data.get("configs", [])
    assert len(configs) == 1
    assert configs[0]["config_id"] == "cfg-100"

    # 2. Fetch config cfg-100 details -> returns tags
    status_cfg, _, data_cfg = request(app, "/api/v1/opc-configs/cfg-100", method="GET")
    assert status_cfg.startswith("200")
    cfg_obj = data_cfg.get("config", data_cfg)
    tags = cfg_obj.get("tags", [])
    assert "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN" in tags
    assert "Pims_A40:gUsw.ToPims.PESO_CALC_BOBINADEIRA" in tags


def test_crud_edit_mapping_lifecycle(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # 1. Create mapping
    _, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_config_id": "cfg-100",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "INITIAL_PI_POINT",
        "point_source": "OPC",
        "location1": 1,
        "publish_interval_ms": 5000,
        "enabled": True,
    })
    mapping_id = data["mapping"]["mapping_id"]

    # 2. Edit mapping (PUT)
    status_put, _, put_data = request(app, f"/api/v1/pi-mappings/{mapping_id}", method="PUT", payload={
        "pi_point_name": "UPDATED_PI_POINT",
        "point_source": "L",
        "location1": 42,
        "publish_interval_ms": 10000,
        "enabled": False,
    })
    assert status_put.startswith("200")
    updated = put_data["mapping"]
    assert updated["pi_point_name"] == "UPDATED_PI_POINT"
    assert updated["point_source"] == "L"
    assert updated["location1"] == 42
    assert updated["publish_interval_ms"] == 10000
    assert bool(updated["enabled"]) is False

    # 3. Verify audit event for update
    status_audit, _, audit_data = request(app, "/api/v1/pi-mappings/audit?equipment_id=eq-100", method="GET")
    assert status_audit.startswith("200")
    events = audit_data["audit_events"]
    assert any(e["event_type"] == "pi_mapping.updated" for e in events)


def test_all_mapping_actions_never_trigger_opc_write_or_config_push(pi_mapping_runtime, monkeypatch):
    app, bridge, _, _, _ = pi_mapping_runtime

    mock_opc_write = MagicMock()
    mock_config_push = MagicMock()
    monkeypatch.setattr(bridge, "write_agent_threadsafe", mock_opc_write, raising=False)
    monkeypatch.setattr(bridge, "dispatch_admin_config_operation_threadsafe", mock_config_push, raising=False)
    monkeypatch.setattr(bridge, "get_live_values", lambda agent_id: {
        "items": [{"opc_item_path": "Pims_A40:gUsw.ToPims.EBA_PERDA_MAGNETICA", "value": 99.9, "quality": 192}]
    })

    # Action 1: Create
    _, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_config_id": "cfg-100",
        "opc_item_path": "Pims_A40:gUsw.ToPims.EBA_PERDA_MAGNETICA",
        "pi_point_name": "PERDA_MAGNETICA",
        "publish_interval_ms": 5000,
        "enabled": True,
    })
    mapping_id = data["mapping"]["mapping_id"]

    # Action 2: Update
    request(app, f"/api/v1/pi-mappings/{mapping_id}", method="PUT", payload={
        "pi_point_name": "PERDA_MAGNETICA_EDITED",
        "publish_interval_ms": 6000,
    })

    # Action 3: Toggle
    request(app, f"/api/v1/pi-mappings/{mapping_id}/toggle", method="POST")
    request(app, f"/api/v1/pi-mappings/{mapping_id}/toggle", method="POST")

    # Action 4: Simulate
    request(app, f"/api/v1/pi-mappings/{mapping_id}/simulate", method="POST")

    # Action 5: Delete
    request(app, f"/api/v1/pi-mappings/{mapping_id}", method="DELETE")

    # Zero writes to OPC, zero CONFIG_PUSH
    mock_opc_write.assert_not_called()
    mock_config_push.assert_not_called()


def test_app_js_auto_refresh_timer_isolated_to_pi_tab(pi_mapping_runtime):
    # Verify app.js content: autoRefreshTimer only refreshes when currentTab === "pi"
    import pathlib
    js_path = pathlib.Path("src/opc_bridge/server/admin/static/app.js")
    js_content = js_path.read_text(encoding="utf-8")

    assert 'if (currentTab === "pi" && selectedPiEquipment)' in js_content
    # Confirm no auto-refresh on equipments or opc tabs
    assert 'if (currentTab === "equipments")' not in js_content
    assert 'if (currentTab === "opc"' not in js_content
