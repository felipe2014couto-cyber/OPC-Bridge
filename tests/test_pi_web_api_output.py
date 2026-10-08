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
        # Create profile
        repo.add_pi_profile(
            profile_id="prof-unit",
            equipment_id="eq-unit",
            opc_prog_id="ABB.AfwOpcDaSurrogate.1",
            point_source="OPC",
            location1=1,
            enabled=True,
        )
        # Create a mapping
        repo.add_pi_mapping(
            mapping_id="map-unit-1",
            equipment_id="eq-unit",
            opc_prog_id="ABB.AfwOpcDaSurrogate.1",
            opc_item_path="TAG.BOBINADEIRA.PESO",
            pi_point_name="PI_BOBIN_PESO",
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
        if k.startswith("OPC_BRIDGE_PI"):
            monkeypatch.delenv(k, raising=False)

    cfg = PiOutputConfig.load_from_env()
    assert cfg.enabled is False
    assert cfg.mode == "simulated"
    assert cfg.base_url == ""
    assert cfg.data_server == "PIMS"
    assert cfg.timeout_seconds == 60.0
    assert cfg.verify_ssl is True

    # Custom environment variables using the exact target schema
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_ENABLED", "true")
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_MODE", "web_api")
    monkeypatch.setenv("OPC_BRIDGE_PIWEBAPI_BASE_URL", "https://piwebapi.corp.local/piwebapi/")
    monkeypatch.setenv("OPC_BRIDGE_PI_SERVER", "PIMS")
    monkeypatch.setenv("OPC_BRIDGE_PIWEBAPI_USERNAME", "pi_user")
    monkeypatch.setenv("OPC_BRIDGE_PIWEBAPI_PASSWORD", "SuperSecret123!")
    monkeypatch.setenv("OPC_BRIDGE_PIWEBAPI_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("OPC_BRIDGE_PIWEBAPI_CA_FILE", "/etc/ssl/ca.pem")
    monkeypatch.setenv("OPC_BRIDGE_PIWEBAPI_VERIFY_SSL", "true")

    cfg_custom = PiOutputConfig.load_from_env()
    assert cfg_custom.enabled is True
    assert cfg_custom.mode == "web_api"
    assert cfg_custom.base_url == "https://piwebapi.corp.local/piwebapi"
    assert cfg_custom.data_server == "PIMS"
    assert cfg_custom.username == "pi_user"
    assert cfg_custom.password == "SuperSecret123!"
    assert cfg_custom.timeout_seconds == 45.0
    assert cfg_custom.ca_bundle == "/etc/ssl/ca.pem"
    assert cfg_custom.verify_ssl is True


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

    # 3. Successful publish via 3-step flow: GET /points?path=... then GET /points/{webid}/attributes then POST /streams/{webid}/value?updateOption=Replace
    points_resp = (200, {"WebId": "P0123456789WebId", "Name": "BOBIN_VEL"})
    attrs_resp = (200, {"Items": [{"Name": "pointsource", "Value": "OPC"}, {"Name": "location1", "Value": 1}]})
    write_resp = (202, {"Status": "Created"})

    with patch.object(channel, "_execute_http", side_effect=[points_resp, attrs_resp, write_resp]) as mock_exec:
        pub_res = channel.publish(
            pi_point_name="BOBIN_VEL",
            value=85.2,
            timestamp=datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc),
            quality=192,
            point_source="OPC",
            location1=1,
        )
        assert pub_res.status == "Publicado"
        assert pub_res.value == 85.2
        assert pub_res.error is None
        assert pub_res.details.get("web_api") is True
        assert pub_res.details.get("status_code") == 202
        assert pub_res.details.get("web_id") == "P0123456789WebId"

        # Verify three HTTP calls: GET /points?path=..., GET /points/{WebId}/attributes and POST /streams/{WebId}/value?updateOption=Replace
        assert mock_exec.call_count == 3
        req_resolve = mock_exec.call_args_list[0][0][0]
        assert req_resolve.get_method() == "GET"
        assert "/points?path=" in req_resolve.full_url
        assert "%5C%5CPIMS%5CBOBIN_VEL" in req_resolve.full_url

        req_attrs = mock_exec.call_args_list[1][0][0]
        assert req_attrs.get_method() == "GET"
        assert "/points/P0123456789WebId/attributes" in req_attrs.full_url

        req_write = mock_exec.call_args_list[2][0][0]
        assert req_write.get_method() == "POST"
        assert "/streams/P0123456789WebId/value?updateOption=Replace" in req_write.full_url
        payload_sent = json.loads(req_write.data.decode("utf-8"))
        assert payload_sent == {
            "Timestamp": "2026-10-07T12:00:00+00:00",
            "Value": 85.2,
        }

    # 4. Fail-closed: Mismatch in PointSource or Location1 blocks publish
    channel.clear_cache()
    attrs_mismatch = (200, {"Items": [{"Name": "pointsource", "Value": "DIFF"}, {"Name": "location1", "Value": 99}]})
    with patch.object(channel, "_execute_http", side_effect=[points_resp, attrs_mismatch]) as mock_exec_fail:
        pub_blocked = channel.publish(
            pi_point_name="BOBIN_VEL",
            value=85.2,
            point_source="OPC",
            location1=1,
        )
        assert pub_blocked.status == "Erro"
        assert pub_blocked.error == 'Publicação bloqueada: o PI Point "BOBIN_VEL" possui Point Source "DIFF", mas este perfil permite somente "OPC".'
        # Stream POST was never called!
        assert mock_exec_fail.call_count == 2


def test_webid_resolution_using_path_and_caching():
    cfg = PiOutputConfig(
        enabled=True,
        mode="web_api",
        base_url="https://piwebapi.corp/piwebapi",
        data_server="PIMS",
    )
    channel = PiWebApiOutputChannel(cfg)

    # 1. First resolution call queries GET /points?path=\\PIMS\\TAG_SPEED
    points_resp = (200, {"WebId": "WEBID_PIMS_SPEED_001", "Name": "TAG_SPEED"})
    with patch.object(channel, "_execute_http", return_value=points_resp) as mock_exec:
        web_id = channel.resolve_point_web_id("TAG_SPEED")
        assert web_id == "WEBID_PIMS_SPEED_001"
        assert mock_exec.call_count == 1
        req = mock_exec.call_args[0][0]
        assert req.get_method() == "GET"
        assert "/points?path=" in req.full_url
        assert "%5C%5CPIMS%5CTAG_SPEED" in req.full_url

    # 2. Second resolution call uses in-memory cache without making any HTTP call
    with patch.object(channel, "_execute_http") as mock_exec_cached:
        web_id_cached = channel.resolve_point_web_id("TAG_SPEED")
        assert web_id_cached == "WEBID_PIMS_SPEED_001"
        assert mock_exec_cached.call_count == 0


def test_publish_workflow_post_streams_value_update_option_replace():
    cfg = PiOutputConfig(
        enabled=True,
        mode="web_api",
        base_url="https://piwebapi.corp/piwebapi",
        data_server="PIMS",
    )
    channel = PiWebApiOutputChannel(cfg)

    # Mock sequence: 1st call GET /points, 2nd call GET /points/{webid}/attributes, 3rd call POST /streams/{webid}/value?updateOption=Replace
    points_resp = (200, {"WebId": "WEBID_PIMS_TAG_99", "Name": "BOBIN_PESO"})
    attrs_resp = (200, {"Items": [{"Name": "pointsource", "Value": "OPC"}, {"Name": "location1", "Value": 1}]})
    stream_resp = (202, {})

    with patch.object(channel, "_execute_http", side_effect=[points_resp, attrs_resp, stream_resp]) as mock_exec:
        dt = datetime(2026, 10, 7, 12, 30, 0, tzinfo=timezone.utc)
        res = channel.publish("BOBIN_PESO", 1234.5, timestamp=dt, point_source="OPC", location1=1)

        assert res.status == "Publicado"
        assert res.value == 1234.5
        assert res.details.get("web_id") == "WEBID_PIMS_TAG_99"
        assert mock_exec.call_count == 3

        # Verify call 1: GET /points?path=\\PIMS\\BOBIN_PESO
        req_resolve = mock_exec.call_args_list[0][0][0]
        assert req_resolve.get_method() == "GET"
        assert "/points?path=%5C%5CPIMS%5CBOBIN_PESO" in req_resolve.full_url

        # Verify call 2: GET /points/WEBID_PIMS_TAG_99/attributes
        req_attrs = mock_exec.call_args_list[1][0][0]
        assert req_attrs.get_method() == "GET"
        assert "/points/WEBID_PIMS_TAG_99/attributes" in req_attrs.full_url

        # Verify call 3: POST /streams/{webid}/value?updateOption=Replace
        req_post = mock_exec.call_args_list[2][0][0]
        assert req_post.get_method() == "POST"
        assert "/streams/WEBID_PIMS_TAG_99/value?updateOption=Replace" in req_post.full_url
        assert req_post.headers["Content-type"] == "application/json"

        # Verify body contains Timestamp and Value
        sent_body = json.loads(req_post.data.decode("utf-8"))
        assert sent_body["Timestamp"] == "2026-10-07T12:30:00+00:00"
        assert sent_body["Value"] == 1234.5

    # Repeat call for the same tag: cache skips GET /points and GET /attributes, only POST /streams is called
    with patch.object(channel, "_execute_http", return_value=stream_resp) as mock_exec_cached:
        res2 = channel.publish("BOBIN_PESO", 1235.0, timestamp=dt, point_source="OPC", location1=1)
        assert res2.status == "Publicado"
        assert mock_exec_cached.call_count == 1
        req_cached = mock_exec_cached.call_args[0][0]
        assert "/streams/WEBID_PIMS_TAG_99/value?updateOption=Replace" in req_cached.full_url


def test_basic_auth_in_memory_only_and_no_secrets_in_logs_or_errors(caplog):
    cfg = PiOutputConfig(
        enabled=True,
        mode="web_api",
        base_url="https://piwebapi.corp/piwebapi",
        username="pi_admin_svc",
        password="TopSecretPasswordXYZ!",
    )
    channel = PiWebApiOutputChannel(cfg)

    # 1. Authorization header built in memory
    req = channel._build_request("https://piwebapi.corp/piwebapi/system/landing", "GET")
    auth_header = req.headers.get("Authorization")
    assert auth_header is not None
    assert auth_header.startswith("Basic ")

    # 2. No password leaked in logs or error representations
    import io
    err_401 = urllib.error.HTTPError(
        url="https://piwebapi.corp/piwebapi/points?path=%5C%5CPIMS%5CTAG",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b"{}"),
    )
    with patch.object(channel, "_execute_http", side_effect=err_401):
        res = channel.publish("TAG_SECRET", 100.0)
        assert res.status == "Erro"
        assert "TopSecretPasswordXYZ!" not in res.error
        assert "TopSecretPasswordXYZ!" not in caplog.text
        assert "Authorization" not in caplog.text


def test_kill_switch_total_blocking_guarantee():
    cfg = PiOutputConfig(
        enabled=False,
        mode="web_api",
        base_url="https://piwebapi.corp/piwebapi",
        username="pi_user",
        password="pi_password",
    )
    channel = PiWebApiOutputChannel(cfg)

    # Total blocking: publish, test_connection, and _execute_http
    pub_res = channel.publish("TAG_ANY", 50.0)
    assert pub_res.status == "Desabilitado"
    assert pub_res.error == "Saída PI desabilitada"

    test_res = channel.test_connection()
    assert test_res["connected"] is False
    assert test_res["error"] == "output_disabled"

    with pytest.raises(PiOutputDisabledError):
        channel._execute_http(MagicMock())


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
        point_source="OPC",
        location1=1,
    )

    # 1. Success mock: resolve WebId, get attributes, then post stream value
    mock_resp_resolve = MagicMock()
    mock_resp_resolve.status = 200
    mock_resp_resolve.getcode.return_value = 200
    mock_resp_resolve.read.return_value = b'{"WebId": "WEBID_SPEED_01"}'
    mock_resp_resolve.__enter__.return_value = mock_resp_resolve

    mock_resp_attrs = MagicMock()
    mock_resp_attrs.status = 200
    mock_resp_attrs.getcode.return_value = 200
    mock_resp_attrs.read.return_value = b'{"Items": [{"Name": "pointsource", "Value": "OPC"}, {"Name": "location1", "Value": 1}]}'
    mock_resp_attrs.__enter__.return_value = mock_resp_attrs

    mock_resp_post = MagicMock()
    mock_resp_post.status = 202
    mock_resp_post.getcode.return_value = 202
    mock_resp_post.read.return_value = b"{}"
    mock_resp_post.__enter__.return_value = mock_resp_post

    with patch("urllib.request.urlopen", side_effect=[mock_resp_resolve, mock_resp_attrs, mock_resp_post]):
        res = pub.publish_single(payload)
        assert res.status == "published"
        assert res.http_status == 202

    # 2. HTTP 404 Point Not Found error mock
    mock_err = urllib.error.HTTPError(
        url="http://10.247.224.39/piwebapi/points?path=%5C%5CPIMS%5CSPEED_TAG_ERR",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=io.BytesIO(b"{}"),
    )
    with patch("urllib.request.urlopen", side_effect=mock_err):
        payload_err = PiValuePayload(opc_item_path="Line1.Speed", pi_point="SPEED_TAG_ERR", value=500.0)
        res_err = pub.publish_single(payload_err)
        assert res_err.status == "error"
        assert "não encontrado no servidor PIMS" in res_err.error
        assert res_err.http_status == 404

    # 3. HTTP 401 Unauthorized mock
    mock_auth_err = urllib.error.HTTPError(
        url="http://10.247.224.39/piwebapi/points?path=%5C%5CPIMS%5CSPEED_TAG_AUTH",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b"{}"),
    )
    with patch("urllib.request.urlopen", side_effect=mock_auth_err):
        payload_auth = PiValuePayload(opc_item_path="Line1.Speed", pi_point="SPEED_TAG_AUTH", value=500.0)
        res_auth = pub.publish_single(payload_auth)
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
    assert "desabilitada" in data["banner_text"].lower()

    # 2. Output enabled
    monkeypatch.setenv("OPC_BRIDGE_PI_OUTPUT_ENABLED", "true")
    st2, _, data2 = request(app, "/api/v1/pi-integration/status")
    assert st2.startswith("200")
    assert data2["output_enabled"] is True
    assert data2["banner_text"] == "Saída PI habilitada"


def test_admin_api_test_connection_endpoint(pi_runtime):
    app, _, _, _, _ = pi_runtime

    # 1. By default, output is disabled -> connected=False, error=output_disabled
    st, _, data = request(app, "/api/v1/pi-integration/test-connection", method="POST")
    assert st.startswith("200")
    assert data["connected"] is False
    assert data["error"] == "output_disabled"

    # 2. When enabled in simulated mode -> connected=True
    with patch.dict(os.environ, {"OPC_BRIDGE_PI_OUTPUT_ENABLED": "true"}):
        st2, _, data2 = request(app, "/api/v1/pi-integration/test-connection", method="POST")
        assert st2.startswith("200")
        assert data2["connected"] is True
        assert "simulado" in data2["message"].lower() or "verificada" in data2["message"].lower()

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


def test_strict_attribute_validation_divergence_messages_and_zero_post():
    cfg = PiOutputConfig(
        enabled=True,
        mode="web_api",
        base_url="http://10.247.224.39/piwebapi",
        data_server="PIMS",
    )
    channel = PiWebApiOutputChannel(cfg)

    points_resp = (200, {"WebId": "WEBID_PIMS_TEST_01", "Name": "TEST_POINT"})
    attrs_match = (200, {"Items": [{"Name": "pointsource", "Value": "S"}, {"Name": "location1", "Value": 4}]})
    write_resp = (202, {"Status": "Created"})

    # 1. Matching attributes: allows POST and validates payload format
    with patch.object(channel, "_execute_http", side_effect=[points_resp, attrs_match, write_resp]) as mock_exec:
        res = channel.publish(
            pi_point_name="TEST_POINT",
            value=42.5,
            timestamp=datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc),
            point_source="S",
            location1=4,
        )
        assert res.status == "Publicado"
        assert res.error is None
        assert mock_exec.call_count == 3
        # Verify POST stream call
        req_post = mock_exec.call_args_list[2][0][0]
        assert req_post.get_method() == "POST"
        assert "/streams/WEBID_PIMS_TEST_01/value?updateOption=Replace" in req_post.full_url
        payload = json.loads(req_post.data.decode("utf-8"))
        assert payload == {"Timestamp": "2026-10-08T12:00:00+00:00", "Value": 42.5}

    # 2. Point Source divergence (actual "O", expected "S"): BLOCKS before any POST
    channel.clear_cache()
    attrs_ps_diff = (200, {"Items": [{"Name": "pointsource", "Value": "O"}, {"Name": "location1", "Value": 4}]})
    with patch.object(channel, "_execute_http", side_effect=[points_resp, attrs_ps_diff]) as mock_exec:
        res_ps = channel.publish(
            pi_point_name="TEST_POINT",
            value=42.5,
            point_source="S",
            location1=4,
        )
        assert res_ps.status == "Erro"
        assert res_ps.error == 'Publicação bloqueada: o PI Point "TEST_POINT" possui Point Source "O", mas este perfil permite somente "S".'
        # Proves ZERO POST: only GET /points and GET /attributes were called
        assert mock_exec.call_count == 2
        for call_args in mock_exec.call_args_list:
            req = call_args[0][0]
            assert req.get_method() == "GET"

    # 3. Location1 divergence (actual 0, expected 4): BLOCKS before any POST
    channel.clear_cache()
    attrs_loc_diff = (200, {"Items": [{"Name": "pointsource", "Value": "S"}, {"Name": "location1", "Value": 0}]})
    with patch.object(channel, "_execute_http", side_effect=[points_resp, attrs_loc_diff]) as mock_exec:
        res_loc = channel.publish(
            pi_point_name="TEST_POINT",
            value=42.5,
            point_source="S",
            location1=4,
        )
        assert res_loc.status == "Erro"
        assert res_loc.error == 'Publicação bloqueada: o PI Point "TEST_POINT" possui Location1 "0", mas este perfil permite somente "4".'
        # Proves ZERO POST
        assert mock_exec.call_count == 2
        for call_args in mock_exec.call_args_list:
            assert call_args[0][0].get_method() == "GET"

    # 4. Missing attribute or query failure: BLOCKS before any POST
    channel.clear_cache()
    attrs_empty = (200, {"Items": []})
    with patch.object(channel, "_execute_http", side_effect=[points_resp, attrs_empty]) as mock_exec:
        res_missing = channel.publish(
            pi_point_name="TEST_POINT",
            value=42.5,
            point_source="S",
            location1=4,
        )
        assert res_missing.status == "Erro"
        assert "não possui o atributo Point Source" in res_missing.error
        assert mock_exec.call_count == 2


def test_test_connection_fallback_to_system_endpoint():
    cfg = PiOutputConfig(
        enabled=True,
        mode="web_api",
        base_url="http://10.247.224.39/piwebapi",
    )
    channel = PiWebApiOutputChannel(cfg)

    # When /system/landing raises HTTP 404, it must fall back to /system
    err_404 = urllib.error.HTTPError("http://10.247.224.39/piwebapi/system/landing", 404, "Not Found", {}, None)
    system_ok = (200, {"ProductTitle": "PI Web API 2023 SP1 Patch 1"})

    with patch.object(channel, "_execute_http", side_effect=[err_404, system_ok]) as mock_exec:
        res = channel.test_connection()
        assert res["connected"] is True
        assert res["status_code"] == 200
        assert "PI Web API 2023 SP1 Patch 1" in res["message"]
        assert mock_exec.call_count == 2
        assert "/system/landing" in mock_exec.call_args_list[0][0][0].full_url
        assert "/system" in mock_exec.call_args_list[1][0][0].full_url


def test_unencrypted_credentials_over_http_safety_guard():
    cfg = PiOutputConfig(
        enabled=True,
        mode="web_api",
        base_url="http://10.247.224.39/piwebapi",
        username="piadmin",
        password="secretpassword",
    )
    channel = PiWebApiOutputChannel(cfg)

    # Attempting to build request with credentials over unencrypted HTTP must raise RuntimeError
    with pytest.raises(RuntimeError) as exc_info:
        channel._build_request("http://10.247.224.39/piwebapi/system", "GET")
    assert "Segurança" in str(exc_info.value)
    assert "inseguro" in str(exc_info.value)
