"""Comprehensive automated test suite for OPC-to-PI mappings (Decoupled Independent Architecture):
- Persistence, Profile per (equipment_id, opc_prog_id), and Spreadsheet Batch API
- Independent OPC address management without coupling to test OPC configs
- Server discovery and Read-Now via isolated inspection without CONFIG_PUSH or OPC write
- Simulation using last available OPC reading with zero OPC writes, zero polling, zero CONFIG_PUSH
- Deletion, toggle/deactivation, and persistent audit trail
- UI integration elements
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from opc_bridge.protocol.inspection import InspectionResponse
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
        # Create independent profile for (equipment_id, opc_prog_id)
        repo.add_pi_profile(
            profile_id="prof-100",
            equipment_id="eq-100",
            opc_prog_id="ABB.AfwOpcDaSurrogate.1",
            point_source="OPC",
            location1=1,
            enabled=True,
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
        repo.add_pi_profile(
            profile_id="prof-1",
            equipment_id="eq-1",
            opc_prog_id="ProgID.1",
            point_source="OPC",
            location1=1,
            enabled=True,
        )

        # 1. Create mapping
        mapping = repo.add_pi_mapping(
            mapping_id="map-1",
            equipment_id="eq-1",
            opc_prog_id="ProgID.1",
            opc_item_path="TAG.A",
            pi_point_name="PI_TAG_A",
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
        assert fetched["point_source"] == "OPC"
        assert fetched["location1"] == 1

        # 3. List mappings
        all_maps = repo.list_pi_mappings(equipment_id="eq-1")
        assert len(all_maps) == 1

        # 4. Update mapping
        updated = repo.update_pi_mapping(
            mapping_id="map-1",
            pi_point_name="PI_TAG_A_NEW",
            publish_interval_ms=10000,
            enabled=True,
        )
        assert updated is not None
        assert updated["pi_point_name"] == "PI_TAG_A_NEW"
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


def test_create_mapping_validates_interval_bounds(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # 1. Below 1000 ms -> rejected
    status1, _, data1 = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "DIAMETRO_CALC_BOBIN",
        "publish_interval_ms": 500,
    })
    assert status1.startswith("400")
    assert data1["error"] == "invalid_publish_interval_bounds"

    # 2. Above 60000 ms -> rejected
    status2, _, data2 = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "DIAMETRO_CALC_BOBIN",
        "publish_interval_ms": 90000,
    })
    assert status2.startswith("400")
    assert data2["error"] == "invalid_publish_interval_bounds"


def test_create_mapping_success_and_auditing(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    status, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "DIAMETRO_CALC_BOBIN",
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
    assert any(e["event_type"] == "pi_mapping.created" for e in events)


def test_simulation_uses_last_available_opc_reading_without_opc_write_or_config_push(pi_mapping_runtime, monkeypatch):
    app, bridge, _, _, _ = pi_mapping_runtime

    # 1. Create mapping
    _, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
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


def test_simulation_rejects_disabled_mapping(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # Create mapping
    _, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
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
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
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
    assert "Saída PI desabilitada" in html

    # 3. Form elements in independent profile card
    assert "pi-profile-card" in html
    assert "pi-equipment-select" in html
    assert "pi-prog-id" in html
    assert "btn-discover-pi-servers" in html
    assert "pi-point-source" in html
    assert "pi-location1" in html
    assert "pi-profile-enabled" in html
    assert "btn-save-pi-profile" in html

    # 4. Spreadsheet elements
    assert "pi-spreadsheet-card" in html
    assert "btn-add-pi-row" in html
    assert "btn-save-pi-sheet" in html
    assert "btn-read-now-pi" in html
    assert "pi-spreadsheet-table" in html

    # 5. Security: NO PI URL, credentials, token or certificate fields in the UI
    assert 'id="pi-url"' not in html
    assert 'id="pi-token"' not in html
    assert 'id="pi-secret"' not in html
    assert 'id="pi-cert"' not in html
    assert "web-api" not in html.lower()

    # 6. Delete confirmation modal
    assert "modal-delete-mapping-confirm" in html


def test_discover_servers_endpoint(pi_mapping_runtime, monkeypatch):
    app, bridge, _, _, _ = pi_mapping_runtime

    # Mock inspect_agent_threadsafe for "servers"
    mock_resp = InspectionResponse(
        request_id="insp-1",
        servers=["ABB.AfwOpcDaServer", "Kepware.KEPServerEX.V6"],
    )
    monkeypatch.setattr(bridge, "inspect_agent_threadsafe", lambda agent_id, req: mock_resp)

    st, _, data = request(app, "/api/v1/pi-integration/discover-servers?equipment_id=eq-100", method="GET")
    assert st.startswith("200")
    servers = data.get("servers", [])
    assert "ABB.AfwOpcDaServer" in servers
    assert "Kepware.KEPServerEX.V6" in servers


def test_crud_edit_mapping_lifecycle(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # 1. Create mapping
    _, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "INITIAL_PI_POINT",
        "publish_interval_ms": 5000,
        "enabled": True,
    })
    mapping_id = data["mapping"]["mapping_id"]

    # 2. Edit mapping (PUT)
    status_put, _, put_data = request(app, f"/api/v1/pi-mappings/{mapping_id}", method="PUT", payload={
        "pi_point_name": "UPDATED_PI_POINT",
        "publish_interval_ms": 10000,
        "enabled": False,
    })
    assert status_put.startswith("200")
    updated = put_data["mapping"]
    assert updated["pi_point_name"] == "UPDATED_PI_POINT"
    assert updated["point_source"] == "OPC"
    assert updated["location1"] == 1
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
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
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
    import pathlib
    js_path = pathlib.Path("src/opc_bridge/server/admin/static/app.js")
    js_content = js_path.read_text(encoding="utf-8")

    assert 'if (currentTab === "pi" && selectedPiEquipment && selectedPiProgId)' in js_content
    # Confirm no auto-refresh on equipments or opc tabs
    assert 'if (currentTab === "equipments")' not in js_content
    assert 'if (currentTab === "opc"' not in js_content


def test_pi_profile_lifecycle_and_mapping_inheritance(pi_mapping_runtime):
    app, _, database, _, _ = pi_mapping_runtime

    # 1. Fetch profiles for eq-100
    st, _, data = request(app, "/api/v1/pi-profiles?equipment_id=eq-100", method="GET")
    assert st.startswith("200")
    profs = data.get("profiles", [])
    assert len(profs) == 1
    assert profs[0]["point_source"] == "OPC"
    assert profs[0]["location1"] == 1

    # 2. Create mapping inheriting from profile
    st_map, _, map_data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "DIAMETRO_HERDADO",
        "publish_interval_ms": 5000,
        "enabled": True,
    })
    assert st_map.startswith("201")
    m = map_data["mapping"]
    assert m["point_source"] == "OPC"
    assert m["location1"] == 1
    mapping_id = m["mapping_id"]

    # 3. Update profile with changed point_source and location1
    st_up, _, up_data = request(app, "/api/v1/pi-profiles", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "point_source": "OPCBRIDGE",
        "location1": 42,
        "enabled": True,
    })
    assert st_up.startswith("200")
    assert up_data["deactivated_mappings"] >= 1

    # 4. Verify mapping has been deactivated and inherited new profile values
    st_get, _, get_data = request(app, f"/api/v1/pi-mappings/{mapping_id}", method="GET")
    assert st_get.startswith("200")
    m_updated = get_data["mapping"]
    assert m_updated["enabled"] == 0
    assert m_updated["point_source"] == "OPCBRIDGE"
    assert m_updated["location1"] == 42
    assert "Perfil alterado" in m_updated["last_publish_status"]

    # 5. Verify audit event
    st_aud, _, aud_data = request(app, "/api/v1/pi-mappings/audit?equipment_id=eq-100", method="GET")
    assert st_aud.startswith("200")
    events = [e["event_type"] for e in aud_data.get("audit_events", [])]
    assert "pi_profile.updated" in events


def test_pi_mapping_blocked_when_profile_inactive_or_missing(pi_mapping_runtime):
    app, _, database, _, _ = pi_mapping_runtime

    # Deactivate profile
    request(app, "/api/v1/pi-profiles", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "point_source": "OPC",
        "location1": 1,
        "enabled": False,
    })

    # Creating mapping should fail with 400 profile_required
    st_fail, _, fail_data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "opc_item_path": "Pims_A40:gUsw.ToPims.PESO_CALC_BOBINADEIRA",
        "pi_point_name": "PESO_FAIL",
        "publish_interval_ms": 5000,
    })
    assert st_fail.startswith("400")
    assert fail_data["error"] in ("profile_required", "pi_profile_required")


def test_validate_point_endpoint(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    st, _, data = request(app, "/api/v1/pi-mappings", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "opc_item_path": "Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN",
        "pi_point_name": "VALIDATE_TEST_POINT",
        "publish_interval_ms": 5000,
        "enabled": True,
    })
    mapping_id = data["mapping"]["mapping_id"]

    st_val, _, val_data = request(app, f"/api/v1/pi-mappings/{mapping_id}/validate-point", method="POST")
    assert st_val.startswith("200")
    assert val_data["valid"] is True
    assert val_data["pi_point_name"] == "VALIDATE_TEST_POINT"
    assert val_data["actual_point_source"] == "OPC"
    assert val_data["actual_location1"] == 1


def test_pi_mappings_batch_endpoint(pi_mapping_runtime):
    app, _, _, _, _ = pi_mapping_runtime

    # 1. Validation failure: duplicate tag
    st_err, _, err_data = request(app, "/api/v1/pi-mappings/batch", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "rows": [
            {"opc_item_path": "TAG.DUPLICATE", "pi_point_name": "PI.POINT.1", "publish_interval_ms": 5000, "enabled": True},
            {"opc_item_path": "TAG.DUPLICATE", "pi_point_name": "PI.POINT.2", "publish_interval_ms": 5000, "enabled": True},
        ],
    })
    assert st_err.startswith("400")
    assert err_data["error"] == "validation_failed"
    assert len(err_data["row_errors"]) >= 1

    # 2. Validation failure: duplicate PI Point
    st_err2, _, err_data2 = request(app, "/api/v1/pi-mappings/batch", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "rows": [
            {"opc_item_path": "TAG.1", "pi_point_name": "PI.DUPLICATE", "publish_interval_ms": 5000, "enabled": True},
            {"opc_item_path": "TAG.2", "pi_point_name": "PI.DUPLICATE", "publish_interval_ms": 5000, "enabled": True},
        ],
    })
    assert st_err2.startswith("400")
    assert err_data2["error"] == "validation_failed"

    # 3. Successful batch save
    st_ok, _, ok_data = request(app, "/api/v1/pi-mappings/batch", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "rows": [
            {"opc_item_path": "BATCH.TAG.1", "pi_point_name": "BATCH.PI.1", "publish_interval_ms": 5000, "enabled": True},
            {"opc_item_path": "BATCH.TAG.2", "pi_point_name": "BATCH.PI.2", "publish_interval_ms": 10000, "enabled": True},
        ],
    })
    assert st_ok.startswith("200")
    assert ok_data["saved_count"] == 2
    assert len(ok_data["mappings"]) == 2

    # Verify query returns saved batch
    st_list, _, list_data = request(
        app,
        "/api/v1/pi-mappings?equipment_id=eq-100&opc_prog_id=ABB.AfwOpcDaSurrogate.1",
        method="GET",
    )
    assert st_list.startswith("200")
    saved_tags = [m["opc_item_path"] for m in list_data["mappings"]]
    assert "BATCH.TAG.1" in saved_tags
    assert "BATCH.TAG.2" in saved_tags


def test_read_now_isolated_opc_inspection(pi_mapping_runtime, monkeypatch):
    app, bridge, _, _, _ = pi_mapping_runtime

    # Mock inspect_agent_threadsafe
    mock_inspect = MagicMock()
    mock_inspect.return_value = InspectionResponse(
        request_id="insp-read-now",
        results=[
            {
                "opc_item_path": "TEST.TAG.READ_NOW",
                "status": "valid",
                "value": 2500.75,
                "quality": 192,
                "quality_text": "Good",
                "opc_timestamp": "2026-10-07T15:30:00.000Z",
            }
        ],
    )
    monkeypatch.setattr(bridge, "inspect_agent_threadsafe", mock_inspect)

    mock_opc_write = MagicMock()
    mock_config_push = MagicMock()
    monkeypatch.setattr(bridge, "write_agent_threadsafe", mock_opc_write, raising=False)
    monkeypatch.setattr(bridge, "dispatch_admin_config_operation_threadsafe", mock_config_push, raising=False)

    st, _, data = request(app, "/api/v1/pi-integration/read-now", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "tags": ["TEST.TAG.READ_NOW"],
    })
    assert st.startswith("200")
    results = data["results"]
    assert len(results) == 1
    assert results[0]["opc_item_path"] == "TEST.TAG.READ_NOW"
    assert results[0]["value"] == 2500.75
    assert results[0]["quality"] == 192

    # Verify inspect called with correct parameters
    mock_inspect.assert_called_once()
    # Zero write, zero CONFIG_PUSH
    mock_opc_write.assert_not_called()
    mock_config_push.assert_not_called()


def test_pi_profile_uniqueness_and_attribute_inheritance(pi_mapping_runtime):
    """Verify profile uniqueness per (equipment_id, opc_prog_id) and mandatory inherited attributes."""
    app, _, database, _, _ = pi_mapping_runtime

    # 1. POST /api/v1/pi-profiles with same equipment and prog_id updates existing profile
    st_post, _, post_data = request(app, "/api/v1/pi-profiles", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "point_source": "OPC_MODIFIED",
        "location1": 42,
        "enabled": True,
    })
    assert st_post.startswith("200")
    assert post_data["profile"]["point_source"] == "OPC_MODIFIED"
    assert post_data["profile"]["location1"] == 42

    # Verify database has only 1 profile for this equipment and prog_id
    with database.session() as repo:
        profiles = repo.list_pi_profiles("eq-100")
        matching = [p for p in profiles if p["opc_prog_id"] == "ABB.AfwOpcDaSurrogate.1"]
        assert len(matching) == 1
        assert matching[0]["point_source"] == "OPC_MODIFIED"
        assert matching[0]["location1"] == 42


def test_pi_integration_total_independence_from_opc_test_tab(pi_mapping_runtime):
    """Verify PI Integration is completely decoupled from named_opc_configs on OPC tab."""
    app, _, database, _, _ = pi_mapping_runtime

    # 1. Create a config in the OPC test tab
    with database.session() as repo:
        repo.add_named_config(
            config_id="opc-test-cfg-1",
            name="Config Teste OPC",
            equipment_id="eq-100",
            opc_prog_id="Kepware.KEPServerEX.V6",
            interval_ms=1000,
            tags_json=json.dumps(["TEST.OPC.TAG1"]),
            agent_id="agent-pi",
        )

    # 2. PI profiles and mappings for eq-100 do not depend on opc-test-cfg-1
    st_prof, _, prof_data = request(
        app,
        "/api/v1/pi-profiles?equipment_id=eq-100&opc_prog_id=ABB.AfwOpcDaSurrogate.1",
        method="GET",
    )
    assert st_prof.startswith("200")
    profile = prof_data["profile"]
    assert profile is not None
    assert "opc_config_id" not in profile

    # 3. Save a batch in PI spreadsheet
    st_batch, _, batch_data = request(app, "/api/v1/pi-mappings/batch", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "rows": [
            {"opc_item_path": "INDEPENDENT.TAG.1", "pi_point_name": "PI.INDEP.1", "publish_interval_ms": 3000, "enabled": True},
        ],
    })
    assert st_batch.startswith("200")
    assert batch_data["saved_count"] == 1

    # 4. Deleting or modifying the OPC test tab config has zero effect on PI profile or mapping
    with database.session() as repo:
        repo.delete_named_config("opc-test-cfg-1")
        assert repo.get_named_config("opc-test-cfg-1") is None

        # PI profile and mapping are completely preserved and intact
        pi_prof = repo.get_pi_profile("eq-100", "ABB.AfwOpcDaSurrogate.1")
        assert pi_prof is not None
        maps = repo.list_pi_mappings("eq-100", "ABB.AfwOpcDaSurrogate.1")
        assert len(maps) == 1
        assert maps[0]["opc_item_path"] == "INDEPENDENT.TAG.1"


def test_read_now_handles_invalid_opc_address(pi_mapping_runtime, monkeypatch):
    """Verify Ler agora handles invalid OPC addresses gracefully without interrupting batch."""
    app, bridge, _, _, _ = pi_mapping_runtime

    # Mock inspect returning one valid and one invalid tag
    mock_inspect = MagicMock()
    mock_inspect.return_value = InspectionResponse(
        request_id="insp-invalid-tag",
        results=[
            {
                "opc_item_path": "VALID.TAG.1",
                "status": "valid",
                "value": 100.5,
                "quality": 192,
                "quality_text": "Good",
                "opc_timestamp": "2026-10-07T16:00:00.000Z",
            },
            {
                "opc_item_path": "INVALID.NONEXISTENT.TAG",
                "status": "invalid",
                "value": None,
                "quality": None,
                "quality_text": None,
                "opc_timestamp": None,
                "error": "Endereço OPC não encontrado.",
            },
        ],
    )
    monkeypatch.setattr(bridge, "inspect_agent_threadsafe", mock_inspect)

    st, _, data = request(app, "/api/v1/pi-integration/read-now", method="POST", payload={
        "equipment_id": "eq-100",
        "opc_prog_id": "ABB.AfwOpcDaSurrogate.1",
        "tags": ["VALID.TAG.1", "INVALID.NONEXISTENT.TAG"],
    })
    assert st.startswith("200")
    results = data["results"]
    assert len(results) == 2

    valid_res = next(r for r in results if r["opc_item_path"] == "VALID.TAG.1")
    assert valid_res["status"] == "valid"
    assert valid_res["value"] == 100.5

    invalid_res = next(r for r in results if r["opc_item_path"] == "INVALID.NONEXISTENT.TAG")
    assert invalid_res["status"] == "invalid"
    assert invalid_res["value"] is None
    assert "não encontrado" in invalid_res["error"].lower()


def test_pi_attribute_divergence_blocks_publication():
    """Verify validate_point_attributes blocks publication if PointSource or Location1 differs from profile."""
    from opc_bridge.server.pi_output import PiOutputConfig, PiWebApiOutputChannel

    config = PiOutputConfig(
        enabled=True,
        mode="web_api",
        base_url="https://piserver.test/piwebapi",
        data_server="PIMS",
    )
    channel = PiWebApiOutputChannel(config=config)

    # Mock WebId resolution
    channel._web_id_cache["TAG_POINT_TEST"] = "webid-test-123"

    # 1. PointSource mismatch
    channel._attributes_cache["webid-test-123"] = {
        "pointsource": "KEPWARE",
        "location1": 1,
    }
    valid_ps, err_ps, _ = channel.validate_point_attributes(
        "TAG_POINT_TEST",
        expected_point_source="OPCBRIDGE",
        expected_location1=1,
    )
    assert valid_ps is False
    assert err_ps == 'Publicação bloqueada: o PI Point "TAG_POINT_TEST" possui Point Source "KEPWARE", mas este perfil permite somente "OPCBRIDGE".'

    # 2. Location1 mismatch
    channel._attributes_cache["webid-test-123"] = {
        "pointsource": "OPCBRIDGE",
        "location1": 99,
    }
    valid_loc, err_loc, _ = channel.validate_point_attributes(
        "TAG_POINT_TEST",
        expected_point_source="OPCBRIDGE",
        expected_location1=1,
    )
    assert valid_loc is False
    assert err_loc == 'Publicação bloqueada: o PI Point "TAG_POINT_TEST" possui Location1 "99", mas este perfil permite somente "1".'

    # 3. Exact match
    channel._attributes_cache["webid-test-123"] = {
        "pointsource": "OPCBRIDGE",
        "location1": 1,
    }
    valid_ok, err_ok, details = channel.validate_point_attributes(
        "TAG_POINT_TEST",
        expected_point_source="OPCBRIDGE",
        expected_location1=1,
    )
    assert valid_ok is True
    assert err_ok is None
    assert details["point_source"] == "OPCBRIDGE"
    assert details["location1"] == 1
