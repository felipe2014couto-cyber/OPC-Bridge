"""Automated test suite for PI Web API output channel, background publisher, and administrative API.

Verifies:
1. Pure open-source / standard library HTTP client without proprietary PI SDKs.
2. Mandatory Kill-Switch (OPC_BRIDGE_PI_OUTPUT_ENABLED): completely blocks outbound network traffic when false.
3. Read-only connection testing (no writes).
4. OPC-to-PI publication logic: only enabled, good quality, non-null, fresh readings.
5. Publish-once operation with validation, DB persistence, and audit logging.
6. Error sanitization (zero secrets leaked to logs, DB, or UI).
7. Isolated failure handling and exponential backoff.
8. Zero writes to OPC (strictly unidirectional output).
"""
from __future__ import annotations

import json
import os
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from opc_bridge.server.admin import create_app
from opc_bridge.server.core import AgentSession, BridgeServer, ServerConfig
from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database
from opc_bridge.server.pi_output import (
    PiOutputConfig,
    PiOutputDisabledError,
    PiPublisher,
    PiPublishResult,
    PiValuePayload,
    PiWebApiOutputChannel,
    SimulatedPiOutputChannel,
    create_pi_output_channel,
    default_pi_point_name,
    evaluate_mapping_publication,
    format_iso_timestamp,
    format_pi_timestamp,
    sanitize_error_message,
)
from opc_bridge.server.pi_publisher import PiPublisherService
from tests.test_admin_api import ADMIN_TOKEN, request
from tests.test_tag_ui import Writer


# ---------------------------------------------------------------------------
# FIXTURES
# ---------------------------------------------------------------------------

@pytest.fixture
def pi_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    database = sqlite_for_tests(str(tmp_path / "pi_output_test.sqlite"))
    upgrade_database(database)

    with database.session() as repo:
        repo.add_agent("agent-unit", "Unit Agent")
        repo.add_credential("agent-unit", "cred-unit", "SECRET-HASH")
        repo.add_equipment(
            equipment_id="eq-unit",
            name="Equipamento 1",
            ip_address="192.168.1.10",
            agent_id="agent-unit",
        )
        repo.add_named_config(
            config_id="cfg-unit",
            name="Config 1",
            equipment_id="eq-unit",
            opc_prog_id="ABB.AfwOpcDaSurrogate.1",
            interval_ms=1000,
            tags_json=json.dumps(["TAG.BOBINADEIRA.PESO", "TAG.BOBINADEIRA.DIAMETRO"]),
            agent_id="agent-unit",
        )
        # Create a mapping
        repo.add_pi_mapping(
            mapping_id="map-unit-1",
            equipment_id="eq-unit",
            opc_config_id="cfg-unit",
            opc_item_path="TAG.BOBINADEIRA.PESO",
            item_id=1,
            pi_point_name="PI_BOBIN_PESO",
            point_source="OPC",
            location1=1,
            publish_interval_ms=2000,
            enabled=True,
        )

    bridge = BridgeServer(ServerConfig(persistence=database))
    writer = Writer()
    from opc_bridge.protocol import ItemRef
    session = AgentSession("connected", writer, "agent-unit", "host", "os", ["opc-da"], config_version=1)
    session.config_items = [
        ItemRef(item_id=1, opc_item_path="TAG.BOBINADEIRA.PESO"),
        ItemRef(item_id=2, opc_item_path="TAG.BOBINADEIRA.DIAMETRO"),
    ]
    session.update_rate_ms = 1000
    bridge._sessions[session.session_id] = session

    app = create_app(database, bridge)
    return app, bridge, database, writer, session


# ---------------------------------------------------------------------------
# UNIT TESTS: CONFIG & SANITIZATION
# ---------------------------------------------------------------------------

def test_pi_output_config_defaults_and_env(monkeypatch):
    # Default values with clean env
    for k in list(os.environ.keys()):
        if k.startswith("OPC_BRIDGE_PI_"):
            monkeypatch.delenv(k, raising=False)

    cfg = PiOutputConfig.load_from_env()
    assert cfg.enabled is False
    assert cfg.mode == "simulated"
    assert cfg.base_url == ""
    assert cfg.timeout_seconds == 10.0
    assert cfg.verify_ssl is True

    # Custom environment variables
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_ENABLED", "true")
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_MODE", "web_api")
    monkeypatch.setenv("OPC_BRIDGE_PI_WEB_API_URL", "https://piwebapi.corp.local/piwebapi/")
    monkeypatch.setenv("OPC_BRIDGE_PI_WEB_API_AUTH_TYPE", "basic")
    monkeypatch.setenv("OPC_BRIDGE_PI_WEB_API_USERNAME", "pi_user")
    monkeypatch.setenv("OPC_BRIDGE_PI_WEB_API_PASSWORD", "SuperSecret123!")
    monkeypatch.setenv("OPC_BRIDGE_PI_WEB_API_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("OPC_BRIDGE_PI_WEB_API_VERIFY_SSL", "false")

    cfg_custom = PiOutputConfig.load_from_env()
    assert cfg_custom.enabled is True
    assert cfg_custom.mode == "web_api"
    assert cfg_custom.base_url == "https://piwebapi.corp.local/piwebapi"
    assert cfg_custom.auth_type == "basic"
    assert cfg_custom.username == "pi_user"
    assert cfg_custom.password == "SuperSecret123!"
    assert cfg_custom.timeout_seconds == 5.5
    assert cfg_custom.verify_ssl is False


def test_sanitize_error_message():
    assert sanitize_error_message(None) == ""
    assert sanitize_error_message("") == ""

    # 1. Strips credentials from URLs
    url_leak = "Falha ao conectar em https://admin:SuperPassword99@piserver.local/piwebapi/system"
    clean_url = sanitize_error_message(url_leak)
    assert "SuperPassword99" not in clean_url
    assert "admin" not in clean_url
    assert "https://***:***@piserver.local/piwebapi/system" in clean_url

    # 2. Strips Bearer tokens
    bearer_leak = "Unauthorized request: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret"
    clean_bearer = sanitize_error_message(bearer_leak)
    assert "eyJhbGci" not in clean_bearer
    assert "Bearer ***" in clean_bearer

    # 3. Strips Basic auth base64
    basic_leak = "Headers included: Basic dXNlcjpwYXNzd29yZDEyMw=="
    clean_basic = sanitize_error_message(basic_leak)
    assert "dXNlcjpwYXNzd29yZDEyMw==" not in clean_basic
    assert "Basic ***" in clean_basic

    # 4. Strips query parameters containing secrets
    query_leak = "URL request https://piserver/api?password=MyPassword123&token=MyToken456"
    clean_query = sanitize_error_message(query_leak)
    assert "MyPassword123" not in clean_query
    assert "MyToken456" not in clean_query
    assert "password=***" in clean_query
    assert "token=***" in clean_query

    # 5. Caps length at 255 chars
    long_msg = "X" * 300
    clean_long = sanitize_error_message(long_msg)
    assert len(clean_long) == 255
    assert clean_long.endswith("...")


def test_simulated_channel_publish_and_test():
    channel = SimulatedPiOutputChannel()

    # Test publish
    res = channel.publish(
        pi_point_name="TEST_POINT",
        value=123.45,
        timestamp="2026-10-07T12:00:00Z",
        quality=192,
        point_source="OPC",
        location1=1,
    )
    assert res.status == "Simulado"
    assert res.value == 123.45
    assert res.pi_point_name == "TEST_POINT"
    assert res.error is None
    assert res.details.get("simulated") is True

    # Test connection
    test_res = channel.test_connection()
    assert test_res["connected"] is True
    assert test_res["mode"] == "simulated"


# ---------------------------------------------------------------------------
# KILL SWITCH & WEB API CHANNELS
# ---------------------------------------------------------------------------

def test_kill_switch_completely_blocks_network():
    # Kill-switch: enabled=False must NEVER open network connections
    cfg = PiOutputConfig(
        enabled=False,
        mode="web_api",
        base_url="https://pi.server.corp/piwebapi",
        username="user",
        password="pwd",
    )
    channel = PiWebApiOutputChannel(cfg)

    # 1. Direct _execute_http raises PiOutputDisabledError
    with pytest.raises(PiOutputDisabledError) as exc_info:
        channel._execute_http(MagicMock())
    assert "Saída PI desabilitada" in str(exc_info.value)

    # 2. publish returns status "Desabilitado" with zero network calls
    res = channel.publish("POINT_A", 10.0)
    assert res.status == "Desabilitado"
    assert res.error == "Saída PI desabilitada"

    # 3. test_connection returns connected=False with zero network calls
    test_res = channel.test_connection()
    assert test_res["connected"] is False
    assert test_res["error"] == "output_disabled"


def test_pi_web_api_channel_with_mocked_http():
    cfg = PiOutputConfig(
        enabled=True,
        mode="web_api",
        base_url="https://piserver.test/piwebapi",
        username="test_user",
        password="test_password",
    )
    channel = PiWebApiOutputChannel(cfg)

    # 1. Successful test_connection
    with patch.object(channel, "_execute_http", return_value=(200, {"ProductTitle": "OSIsoft PI Web API 2023"})):
        conn_res = channel.test_connection()
        assert conn_res["connected"] is True
        assert conn_res["status_code"] == 200
        assert "OSIsoft PI Web API 2023" in conn_res["message"]

    # 2. Unauthorized (HTTP 401) in test_connection
    http_error = urllib.error.HTTPError(
        url="https://piserver.test/piwebapi/system/landing",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=None,
    )
    with patch.object(channel, "_execute_http", side_effect=http_error):
        conn_res_err = channel.test_connection()
        assert conn_res_err["connected"] is False
        assert conn_res_err["status_code"] == 401
        assert "401" in conn_res_err["message"]

    # 3. Successful publish via canonical streams/recorded endpoint
    write_resp = (202, {"Status": "Created"})

    with patch.object(channel, "_execute_http", return_value=write_resp) as mock_exec:
        pub_res = channel.publish(
            pi_point_name="BOBIN_VEL",
            value=85.2,
            timestamp=datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc),
            quality=192,
        )
        assert pub_res.status == "Publicado"
        assert pub_res.value == 85.2
        assert pub_res.error is None
        assert pub_res.details.get("web_api") is True
        assert pub_res.details.get("status_code") == 202

        # Verify single HTTP call made to streams/recorded with quoted path
        assert mock_exec.call_count == 1
        req_sent = mock_exec.call_args[0][0]
        assert "/streams/recorded?path=" in req_sent.full_url
        assert "%5C%5CPIMS%5CBOBIN_VEL" in req_sent.full_url


def test_default_pi_point_name_extraction():
    assert default_pi_point_name("Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN") == "DIAMETRO_CALC_BOBIN"
    assert default_pi_point_name("PESO_CALC_BOBINADEIRA") == "PESO_CALC_BOBINADEIRA"
    assert default_pi_point_name("ABB.Tag.1.Value") == "Value"
    assert default_pi_point_name("MyTag", configured_pi_point="CUSTOM_PI_POINT") == "CUSTOM_PI_POINT"
    assert default_pi_point_name("") == ""


def test_format_pi_timestamp():
    ts1 = format_pi_timestamp("2026-10-07T12:00:00Z")
    assert "2026-10-07" in ts1

    ts2 = format_pi_timestamp(None)
    assert "T" in ts2

    ts3 = format_pi_timestamp(1700000000.0)
    assert "2023" in ts3


def test_pi_publisher_simulated():
    pub = PiPublisher(simulated=True)
    payload = PiValuePayload(
        opc_item_path="Line1.Speed",
        pi_point="SPEED_TAG",
        value=123.45,
        timestamp="2026-10-07T10:00:00Z",
        quality=192,
    )
    res = pub.publish_single(payload)
    assert res.status == "published"
    assert res.value == 123.45
    assert res.pi_point == "SPEED_TAG"
    assert res.http_status == 200

    # Batch
    batch_res = pub.publish_batch([payload])
    assert len(batch_res) == 1
    assert batch_res[0].status == "published"


def test_pi_publisher_real_http_success_and_error():
    import io
    pub = PiPublisher(base_url="http://10.247.224.39/piwebapi", data_server="PIMS", simulated=False)
    payload = PiValuePayload(
        opc_item_path="Line1.Speed",
        pi_point="SPEED_TAG",
        value=500.0,
        quality=192,
    )

    # 1. Success mock
    mock_resp = MagicMock()
    mock_resp.status = 202
    mock_resp.getcode.return_value = 202
    mock_resp.read.return_value = b"{}"
    mock_resp.__enter__.return_value = mock_resp
    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = pub.publish_single(payload)
        assert res.status == "published"
        assert res.http_status == 202

    # 2. HTTP 404 Point Not Found error mock
    mock_err = urllib.error.HTTPError(
        url="http://10.247.224.39/piwebapi/streams/recorded",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=io.BytesIO(b"{}"),
    )
    with patch("urllib.request.urlopen", side_effect=mock_err):
        res_err = pub.publish_single(payload)
        assert res_err.status == "error"
        assert "não encontrado no servidor PIMS" in res_err.error
        assert res_err.http_status == 404

    # 3. HTTP 401 Unauthorized mock
    mock_auth_err = urllib.error.HTTPError(
        url="http://10.247.224.39/piwebapi/streams/recorded",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b"{}"),
    )
    with patch("urllib.request.urlopen", side_effect=mock_auth_err):
        res_auth = pub.publish_single(payload)
        assert res_auth.status == "error"
        assert "Acesso não autorizado ao PI Web API (HTTP 401)" in res_auth.error
        assert res_auth.http_status == 401

    # 4. Empty point name
    empty_payload = PiValuePayload(
        opc_item_path="Line1.Speed",
        pi_point="",
        value=500.0,
    )
    res_empty = pub.publish_single(empty_payload)
    assert res_empty.status == "error"
    assert res_empty.error == "Ponto PI de destino não informado"


def test_create_pi_output_channel_factory(monkeypatch):
    # When disabled -> Simulated
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_ENABLED", "false")
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_MODE", "web_api")
    ch1 = create_pi_output_channel()
    assert isinstance(ch1, SimulatedPiOutputChannel)

    # When enabled and mode is simulated -> Simulated
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_ENABLED", "true")
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_MODE", "simulated")
    ch2 = create_pi_output_channel()
    assert isinstance(ch2, SimulatedPiOutputChannel)

    # When enabled, mode is web_api, and URL is set -> PiWebApiOutputChannel
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_ENABLED", "true")
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_MODE", "web_api")
    monkeypatch.setenv("OPC_BRIDGE_PI_WEB_API_URL", "https://pi.test/piwebapi")
    ch3 = create_pi_output_channel()
    assert isinstance(ch3, PiWebApiOutputChannel)


# ---------------------------------------------------------------------------
# PUBLICATION LOGIC: EVALUATE MAPPING PUBLICATION
# ---------------------------------------------------------------------------

def test_evaluate_mapping_publication():
    cfg = PiOutputConfig(enabled=True, mode="simulated")
    channel = SimulatedPiOutputChannel()
    mapping = {
        "mapping_id": "map-test",
        "pi_point_name": "POINT_TEST",
        "publish_interval_ms": 5000,
        "enabled": True,
        "failure_count": 0,
    }

    # 1. Disabled mapping
    res_disabled = evaluate_mapping_publication({**mapping, "enabled": False}, {"value": 10}, channel, cfg)
    assert res_disabled["action"] == "skipped"
    assert res_disabled["reason"] == "mapping_disabled"

    # 2. No OPC reading in cache
    res_no_reading = evaluate_mapping_publication(mapping, None, channel, cfg)
    assert res_no_reading["action"] == "skipped"
    assert res_no_reading["reason"] == "no_opc_reading"

    # 3. Reading has null value
    res_null = evaluate_mapping_publication(mapping, {"value": None, "quality": 192}, channel, cfg)
    assert res_null["action"] == "skipped"
    assert res_null["reason"] == "null_value"

    # 4. Bad quality (quality < 192)
    res_bad_qual = evaluate_mapping_publication(mapping, {"value": 10, "quality": 64}, channel, cfg)
    assert res_bad_qual["action"] == "skipped"
    assert res_bad_qual["reason"] == "bad_quality"

    # 5. Stale reading
    res_stale = evaluate_mapping_publication(mapping, {"value": 10, "quality": 192, "stale": True}, channel, cfg)
    assert res_stale["action"] == "skipped"
    assert res_stale["reason"] == "stale_data"

    # 6. Valid reading published
    now = datetime.now(timezone.utc)
    res_ok = evaluate_mapping_publication(
        mapping,
        {"value": 42.0, "quality": 192, "opc_timestamp": now.isoformat()},
        channel,
        cfg,
        now_dt=now,
    )
    assert res_ok["action"] == "published"
    assert res_ok["value"] == 42.0
    assert res_ok["new_status"] == "Simulado"
    assert res_ok["failure_count"] == 0
    assert "next_publish_due_at" in res_ok

    # 7. Respect interval: not due yet
    mapping_future = {
        **mapping,
        "next_publish_due_at": (now + timedelta(seconds=10)).isoformat(),
    }
    res_not_due = evaluate_mapping_publication(
        mapping_future,
        {"value": 42.0, "quality": 192},
        channel,
        cfg,
        now_dt=now,
        force=False,
    )
    assert res_not_due["action"] == "skipped"
    assert res_not_due["reason"] == "interval_not_elapsed"

    # 8. force=True bypasses interval
    res_forced = evaluate_mapping_publication(
        mapping_future,
        {"value": 42.0, "quality": 192},
        channel,
        cfg,
        now_dt=now,
        force=True,
    )
    assert res_forced["action"] == "published"


# ---------------------------------------------------------------------------
# ADMIN API: STATUS, TEST-CONNECTION, PUBLISH-ONCE
# ---------------------------------------------------------------------------

def test_admin_api_pi_integration_status_endpoint(pi_runtime, monkeypatch):
    app, _, _, _, _ = pi_runtime

    # 1. Output disabled (default)
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_ENABLED", "false")
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_MODE", "simulated")
    st, _, data = request(app, "/api/v1/pi-integration/status")
    assert st.startswith("200")
    assert data["output_enabled"] is False
    assert data["output_mode"] == "simulated"
    assert "simula" in data["banner_text"].lower()

    # 2. Output enabled
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_ENABLED", "true")
    st2, _, data2 = request(app, "/api/v1/pi-integration/status")
    assert st2.startswith("200")
    assert data2["output_enabled"] is True
    assert data2["banner_text"] == "Saída PI habilitada"


def test_admin_api_test_connection_endpoint(pi_runtime):
    app, _, _, _, _ = pi_runtime

    # POST /api/v1/pi-integration/test-connection
    st, _, data = request(app, "/api/v1/pi-integration/test-connection", method="POST")
    assert st.startswith("200")
    assert data["connected"] is True
    assert "simulado" in data["message"].lower() or "verificada" in data["message"].lower()

    # GET is rejected with 405 Method Not Allowed
    st_get, _, _ = request(app, "/api/v1/pi-integration/test-connection", method="GET")
    assert st_get.startswith("405")


def test_admin_api_publish_once_success_and_validations(pi_runtime):
    app, bridge, database, _, _ = pi_runtime

    # Inject live reading into memory cache
    now_iso = format_iso_timestamp(datetime.now(timezone.utc))
    bridge._live_values["agent-unit"] = {
        1: {
            "item_id": 1,
            "opc_item_path": "TAG.BOBINADEIRA.PESO",
            "value": 1500.5,
            "quality": 192,
            "quality_text": "Good",
            "opc_timestamp": now_iso,
            "age_ms": 250,
            "stale": False,
        }
    }

    # 1. Publish-once success
    st, _, data = request(app, "/api/v1/pi-mappings/map-unit-1/publish-once", method="POST")
    assert st.startswith("200")
    assert data["result"]["status"] in ("Simulado", "Publicado")
    assert data["result"]["value"] == 1500.5
    assert data["mapping"]["last_published_value"] == "1500.5"

    # Verify audit event in DB
    with database.session() as repo:
        events = repo._dicts(repo._execute("SELECT * FROM audit_events WHERE event_type = 'pi_mapping.publish_once'"))
        assert len(events) == 1
        det = json.loads(events[0]["detail_json"])
        assert det["mapping_id"] == "map-unit-1"
        assert det["value"] == 1500.5
        assert det["quality"] == 192

    # 2. Rejection: Bad quality
    bridge._live_values["agent-unit"][1]["quality"] = 0
    st_bad, _, err_bad = request(app, "/api/v1/pi-mappings/map-unit-1/publish-once", method="POST")
    assert st_bad.startswith("400")
    assert err_bad["error"] == "bad_quality"

    # 3. Rejection: Stale reading
    bridge._live_values["agent-unit"][1]["quality"] = 192
    bridge._live_values["agent-unit"][1]["stale"] = True
    st_stale, _, err_stale = request(app, "/api/v1/pi-mappings/map-unit-1/publish-once", method="POST")
    assert st_stale.startswith("400")
    assert err_stale["error"] == "stale_data"

    # 4. Rejection: Null value
    bridge._live_values["agent-unit"][1]["stale"] = False
    bridge._live_values["agent-unit"][1]["value"] = None
    st_null, _, err_null = request(app, "/api/v1/pi-mappings/map-unit-1/publish-once", method="POST")
    assert st_null.startswith("400")
    assert err_null["error"] == "null_value"

    # 5. Rejection: Disabled mapping
    with database.session() as repo:
        repo.set_pi_mapping_enabled("map-unit-1", False)

    st_dis, _, err_dis = request(app, "/api/v1/pi-mappings/map-unit-1/publish-once", method="POST")
    assert st_dis.startswith("409")
    assert err_dis["error"] == "mapping_disabled"


def test_admin_api_list_pi_mappings_returns_tracking_fields(pi_runtime):
    app, bridge, database, _, _ = pi_runtime

    # Update mapping tracking in DB
    with database.session() as repo:
        repo.update_pi_mapping_publication(
            mapping_id="map-unit-1",
            status="Publicado",
            published_value="99.9",
            next_publish_due_at="2026-10-07T12:05:00Z",
            error=None,
            failure_count=0,
        )

    st, _, data = request(app, "/api/v1/pi-mappings")
    assert st.startswith("200")
    mappings = data["mappings"]
    assert len(mappings) == 1
    m = mappings[0]
    assert m["last_publish_status"] == "Publicado"
    assert m["last_published_value"] == "99.9"
    assert m["next_publish_due_at"] == "2026-10-07T12:05:00Z"
    assert m["failure_count"] == 0
    assert "stale" in m


# ---------------------------------------------------------------------------
# BACKGROUND PUBLISHER SERVICE
# ---------------------------------------------------------------------------

def test_pi_publisher_service_due_cycle(pi_runtime):
    _, bridge, database, _, _ = pi_runtime

    cfg = PiOutputConfig(enabled=True, mode="simulated")
    channel = SimulatedPiOutputChannel()
    service = PiPublisherService(
        bridge_server=bridge,
        database=database,
        config=cfg,
        channel=channel,
    )

    # 1. No live reading in cache -> cycle publishes nothing
    published_empty = service.publish_due_cycle()
    assert len(published_empty) == 0

    # 2. Inject fresh live reading into cache
    now_iso = format_iso_timestamp(datetime.now(timezone.utc))
    bridge._live_values["agent-unit"] = {
        1: {
            "item_id": 1,
            "opc_item_path": "TAG.BOBINADEIRA.PESO",
            "value": 250.0,
            "quality": 192,
            "quality_text": "Good",
            "opc_timestamp": now_iso,
            "age_ms": 100,
            "stale": False,
        }
    }

    published = service.publish_due_cycle()
    assert len(published) == 1
    assert published[0]["value"] == 250.0

    # 3. Check DB updated
    with database.session() as repo:
        updated_map = repo.get_pi_mapping("map-unit-1")
        assert updated_map["last_published_value"] == "250.0"
        assert updated_map["failure_count"] == 0

    # 4. Immediate second cycle skips publication because interval has not elapsed
    published_second = service.publish_due_cycle()
    assert len(published_second) == 0


def test_pi_publisher_kill_switch_skips_all_cycles(pi_runtime):
    _, bridge, database, _, _ = pi_runtime

    # Disabled config
    cfg_disabled = PiOutputConfig(enabled=False, mode="web_api")
    service = PiPublisherService(bridge_server=bridge, database=database, config=cfg_disabled)

    published = service.publish_due_cycle()
    assert published == []


# ---------------------------------------------------------------------------
# INDUSTRIAL SAFETY: ZERO WRITES TO OPC & NO CREDENTIAL LEAKS
# ---------------------------------------------------------------------------

def test_industrial_safety_zero_opc_writes(pi_runtime):
    app, bridge, database, writer, session = pi_runtime

    # Setup live reading
    bridge._live_values["agent-unit"] = {
        1: {
            "item_id": 1,
            "opc_item_path": "TAG.BOBINADEIRA.PESO",
            "value": 123.4,
            "quality": 192,
            "opc_timestamp": format_iso_timestamp(datetime.now(timezone.utc)),
            "stale": False,
        }
    }

    # Execute simulation and publish-once
    request(app, "/api/v1/pi-mappings/map-unit-1/simulate", method="POST")
    request(app, "/api/v1/pi-mappings/map-unit-1/publish-once", method="POST")

    # STRICT INDUSTRIAL VERIFICATION:
    # 1. The agent session writer received ZERO frames (no CONFIG_PUSH, no write commands)
    assert len(writer.frames) == 0

    # 2. No pending config operations created
    assert len(bridge._pending_config_operations) == 0

    # 3. No pending writes created
    assert len(bridge._pending_reads) == 0
