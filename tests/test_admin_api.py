"""Administrative API tests using the WSGI contract."""
from __future__ import annotations

import asyncio
import io
import json
from concurrent.futures import Future
from typing import Any

import pytest

from opc_bridge.protocol import ConfigPushPayload, MsgType, frame_message, unframe_message
from opc_bridge.protocol.messages import ConfigAckPayload
from opc_bridge.server.admin import create_app
from opc_bridge.server.core import AgentSession, BridgeServer, ServerConfig
from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database

ADMIN_TOKEN = "local-admin-token-test-value"
AGENT_SECRET_HASH = "sha256:top-secret-agent-credential-hash"


@pytest.fixture
def database(tmp_path):
    db = sqlite_for_tests(str(tmp_path / "admin.sqlite"))
    upgrade_database(db)
    with db.session() as repo:
        repo.add_agent("agent-a", "Boiler Agent")
        repo.add_credential("agent-a", "credential-a", AGENT_SECRET_HASH)
        repo.add_session("agent-a", "session-a", "boiler-host", "Windows 10")
        repo.update_session_observed(
            "session-a",
            "connected",
            '{"connection":"connected","hostname":"boiler-host",'
            '"os_version":"Windows 10","applied_config_version":2}',
            "2026-10-01T12:00:00Z",
        )
        repo.update_session_applied_version("session-a", 2)
        repo.add_agent("agent-b", "Pump Agent")
        for index, status in enumerate(("applied", "rejected", "expired", "pending"), start=1):
            agent_id = "agent-a" if index < 4 else "agent-b"
            snapshot_id = "snapshot-" + str(index)
            repo.add_snapshot(
                agent_id,
                snapshot_id,
                index,
                json.dumps({
                    "config_version": index, "update_rate_ms": 1000,
                    "opc_prog_id": "", "items": [],
                }),
            )
            operation_id = "operation-" + str(index)
            repo.add_operation(agent_id, operation_id, snapshot_id)
            if status != "pending":
                repo.complete_operation(operation_id, status)
    return db


@pytest.fixture
def app(database, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    return create_app(database)


def request(
    app, path: str, token: str | None = ADMIN_TOKEN, method: str = "GET",
    payload: Any = None, raw_body: bytes | None = None,
):
    status: list[str] = []
    headers: list[list[tuple[str, str]]] = []

    def start_response(response_status: str, response_headers: list[tuple[str, str]]) -> None:
        status.append(response_status)
        headers.append(response_headers)

    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path.split("?", 1)[0],
        "QUERY_STRING": path.partition("?")[2],
        "wsgi.input": io.BytesIO(
            raw_body if raw_body is not None else
            (json.dumps(payload).encode("utf-8") if payload is not None else b"")
        ),
    }
    if payload is not None or raw_body is not None:
        environ["CONTENT_TYPE"] = "application/json"
        environ["CONTENT_LENGTH"] = str(len(environ["wsgi.input"].getvalue()))
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = "Bearer " + token
    body = b"".join(app(environ, start_response))
    return status[0], dict(headers[0]), json.loads(body)


def config_payload(path="Boiler.Area.Pressure"):
    return {"update_rate_ms": 750, "opc_prog_id": "Vendor.OPCServer", "items": [
        {"item_id": 7, "opc_item_path": path, "requested_source": 0}
    ]}


def make_bridge(database, monkeypatch, *, connected=False):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    bridge = BridgeServer(ServerConfig(persistence=database, config_ack_timeout_ms=1000))
    if not connected:
        return create_app(database, bridge), bridge, None

    class Writer:
        def __init__(self):
            self.frames = []

        def write(self, data):
            self.frames.append(data)

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    writer = Writer()
    session = AgentSession("session-a", writer, "agent-a", "host", "Windows", [])
    bridge._sessions[session.session_id] = session
    bridge._running = True
    bridge.dispatch_admin_config_operation_threadsafe = lambda agent_id, operation_id, payload: asyncio.run(
        bridge.dispatch_admin_config_operation(agent_id, operation_id, payload)
    )
    app = create_app(database, bridge)
    return app, bridge, writer


def test_health_requires_admin_bearer_token(app):
    status, headers, body = request(app, "/health")

    assert status == "200 OK"
    assert body == {"status": "ok", "service": "opc-bridge", "version": "0.1.0"}
    assert headers["Cache-Control"] == "no-store"


def test_api_refuses_to_create_app_without_admin_token(database, monkeypatch):
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="ADMIN_API_TOKEN"):
        create_app(database)


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_requests_without_valid_bearer_token_receive_401(app, token):
    status, _, body = request(app, "/health", token)

    assert status == "401 Unauthorized"
    assert body == {"error": "unauthorized"}


def test_agent_list_contains_safe_operational_state_only(app):
    status, _, body = request(app, "/api/v1/agents")

    assert status == "200 OK"
    agent = body["agents"][0]
    assert agent["agent_id"] == "agent-a"
    assert agent["state"] == "connected"
    assert agent["last_heartbeat"] == "2026-10-01T12:00:00Z"
    assert agent["current_session"]["session_id"] == "session-a"
    assert AGENT_SECRET_HASH not in json.dumps(body)
    assert "credential_hash" not in json.dumps(body)
    assert "auth_token" not in json.dumps(body)


def test_agent_detail_and_not_found(app):
    status, _, body = request(app, "/api/v1/agents/agent-a")
    missing_status, _, missing_body = request(app, "/api/v1/agents/does-not-exist")

    assert status == "200 OK"
    assert body["agent"]["display_name"] == "Boiler Agent"
    assert body["agent"]["observed_state"]["hostname"] == "boiler-host"
    assert missing_status == "404 Not Found"
    assert missing_body == {"error": "not_found"}


def test_config_operation_history_and_agent_filter(app):
    status, _, all_body = request(app, "/api/v1/config-operations")
    filtered_status, _, filtered_body = request(
        app, "/api/v1/config-operations?agent_id=agent-a"
    )

    assert status == "200 OK"
    assert {row["status"] for row in all_body["operations"]} == {
        "applied",
        "rejected",
        "expired",
        "pending",
    }
    assert all("version" in row and "requested_at" in row for row in all_body["operations"])
    assert filtered_status == "200 OK"
    assert len(filtered_body["operations"]) == 3
    assert {row["agent_id"] for row in filtered_body["operations"]} == {"agent-a"}
    rejected = next(row for row in filtered_body["operations"] if row["status"] == "rejected")
    assert rejected["error"] == "Agent rejected the configuration."


def test_configuration_operation_requires_authentication(app):
    for token in (None, "wrong-token"):
        status, _, body = request(
            app, "/api/v1/agents/agent-a/config-operations", token,
            "POST", config_payload(),
        )
        assert status == "401 Unauthorized"
        assert body == {"error": "unauthorized"}


def test_malformed_and_duplicate_key_json_is_rejected(database, monkeypatch):
    app, _, _ = make_bridge(database, monkeypatch)
    for raw in (b"{broken", b'{"update_rate_ms":1,"update_rate_ms":2,"items":[]}'):
        status, _, body = request(
            app, "/api/v1/agents/agent-a/config-operations", method="POST", raw_body=raw
        )
        assert status == "400 Bad Request"
        assert body == {"error": "invalid_configuration"}


def test_standalone_read_only_app_does_not_register_configuration_post(database, monkeypatch):
    app, _, _ = make_bridge(database, monkeypatch)
    read_only_app = create_app(database)
    status, _, body = request(
        read_only_app, "/api/v1/agents/agent-a/config-operations",
        method="POST", payload=config_payload(),
    )
    assert status == "405 Method Not Allowed"
    assert body == {"error": "method_not_allowed"}
    assert app._database is database


def test_supported_runtime_shares_bridge_and_database_and_dispatches_config_push(
    database, monkeypatch
):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ADMIN_API_PORT", "0")
    from opc_bridge.server import core

    constructed = []
    original_init = core.BridgeServer.__init__

    def count_bridge_instances(instance, config):
        constructed.append(instance)
        original_init(instance, config)

    monkeypatch.setattr(core.BridgeServer, "__init__", count_bridge_instances)

    class FakeAsyncListener:
        def __init__(self):
            self.sockets = [type("Socket", (), {"getsockname": lambda self: ("127.0.0.1", 8443)})()]

        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def fake_start_server(*args, **kwargs):
        return FakeAsyncListener()

    class FakeAdminHttpd:
        server_port = 8081

        def __init__(self, app):
            self.app = app

        def get_app(self):
            return self.app

        def serve_forever(self):
            return None

        def shutdown(self):
            return None

        def server_close(self):
            return None

    def fake_make_server(host, port, app):
        assert host == "127.0.0.1"
        return FakeAdminHttpd(app)

    monkeypatch.setattr(core.asyncio, "start_server", fake_start_server)
    import wsgiref.simple_server

    monkeypatch.setattr(wsgiref.simple_server, "make_server", fake_make_server)

    class Writer:
        def __init__(self):
            self.frames = []

        def write(self, data):
            self.frames.append(data)

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    server = core.BridgeServer(core.ServerConfig(host="127.0.0.1", port=0, persistence=database))
    try:
        asyncio.run(server.start())
        assert constructed == [server]
        admin_app = server._admin_httpd.get_app()
        assert admin_app._bridge_server is server
        assert admin_app._database is database
        writer = Writer()
        session = AgentSession("runtime-session", writer, "agent-a", "host", "Windows", [])
        server._sessions[session.session_id] = session

        class RunningLoop:
            @staticmethod
            def is_running():
                return True

        dispatch_loop = RunningLoop()
        server._event_loop = dispatch_loop

        def run_coroutine_threadsafe(coroutine, loop):
            assert loop is dispatch_loop
            result = Future()
            result.set_result(asyncio.run(coroutine))
            return result

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", run_coroutine_threadsafe)
        status, _, body = request(
            admin_app, "/api/v1/agents/agent-a/config-operations",
            method="POST", payload=config_payload(),
        )
        assert status == "201 Created"
        assert body["status"] == "pending"
        framed = unframe_message(writer.frames[0])
        assert framed[0].msg_type == MsgType.CONFIG_PUSH
        pushed = ConfigPushPayload.unpack(framed[1])
        assert pushed.config_version == body["version"]
        assert pushed.items[0].opc_item_path == "Boiler.Area.Pressure"
        ack = ConfigAckPayload(pushed.config_version, True).pack()

        async def process_ack():
            reader = asyncio.StreamReader()
            reader.feed_data(frame_message(MsgType.CONFIG_ACK, 1, ack))
            reader.feed_eof()
            server._running = True
            await server._message_loop(session, reader)

        asyncio.run(process_ack())
        with database.session() as repo:
            operation = repo.get_admin_config_operation(body["operation_id"])
        assert operation["status"] == "applied"
    finally:
        server._sessions.clear()
        server._admin_httpd = None
        server._admin_thread = None
        server._server = None


@pytest.mark.parametrize("payload", [
    {"config_version": 44, **config_payload()},
    {**config_payload(), "extra": "x"},
    {"update_rate_ms": True, "items": [{"item_id": 1, "opc_item_path": "A"}]},
    {"update_rate_ms": 0, "items": [{"item_id": 1, "opc_item_path": "A"}]},
    {"update_rate_ms": 10, "items": []},
    {"update_rate_ms": 10, "items": [
        {"item_id": 1, "opc_item_path": "A"}, {"item_id": 1, "opc_item_path": "B"}
    ]},
    {"update_rate_ms": 10, "items": [
        {"item_id": 1, "opc_item_path": "A"}, {"item_id": 2, "opc_item_path": "A"}
    ]},
    {"update_rate_ms": 10, "items": [{"item_id": 1, "opc_item_path": "A", "requested_source": 1}]},
])
def test_invalid_configurations_are_rejected(database, monkeypatch, payload):
    app, _, _ = make_bridge(database, monkeypatch)
    status, _, body = request(app, "/api/v1/agents/agent-a/config-operations", method="POST", payload=payload)
    assert status == "400 Bad Request"
    assert body["error"] == "invalid_configuration"
    with database.session() as repo:
        assert len(repo.list_snapshots("agent-a")) == 3


def test_config_operation_not_found(database, monkeypatch):
    app, _, _ = make_bridge(database, monkeypatch)
    status, _, body = request(app, "/api/v1/agents/missing/config-operations", method="POST", payload=config_payload())
    assert status == "404 Not Found"
    assert body == {"error": "not_found"}


def test_disconnected_agent_creates_pending_immutable_snapshot_and_increments_version(database, monkeypatch):
    app, _, _ = make_bridge(database, monkeypatch)
    first_status, _, first = request(app, "/api/v1/agents/agent-a/config-operations", method="POST", payload=config_payload())
    second_status, _, second = request(app, "/api/v1/agents/agent-a/config-operations", method="POST", payload=config_payload("Boiler.Area.Temp"))
    assert first_status == second_status == "201 Created"
    assert first["version"] == 4 and second["version"] == 5
    assert first["status"] == second["status"] == "pending"
    assert set(first) == {"operation_id", "agent_id", "version", "status"}
    assert "Vendor.OPCServer" not in json.dumps(first)
    assert AGENT_SECRET_HASH not in json.dumps(first)
    with database.session() as repo:
        snapshots = repo.list_snapshots("agent-a")
        operations = repo.list_operations("agent-a")
        assert [s.version for s in snapshots] == [1, 2, 3, 4, 5]
        assert operations[-2].status == operations[-1].status == "pending"
        assert json.loads(snapshots[-1].payload_json)["items"][0]["opc_item_path"] == "Boiler.Area.Temp"


@pytest.mark.parametrize("applied, expected", [(True, "applied"), (False, "rejected")])
def test_connected_agent_uses_config_push_and_ack_updates_history(database, monkeypatch, applied, expected):
    app, bridge, writer = make_bridge(database, monkeypatch, connected=True)
    status, _, response = request(app, "/api/v1/agents/agent-a/config-operations", method="POST", payload=config_payload())
    assert status == "201 Created"
    assert response["status"] == "pending"
    pushed = unframe_message(writer.frames[0])
    assert pushed[0].msg_type == MsgType.CONFIG_PUSH
    decoded = ConfigPushPayload.unpack(pushed[1])
    assert decoded.config_version == response["version"]
    assert decoded.update_rate_ms == 750
    assert decoded.opc_prog_id == "Vendor.OPCServer"
    assert decoded.items[0].opc_item_path == "Boiler.Area.Pressure"
    ack = ConfigAckPayload(decoded.config_version, applied).pack()
    async def process_ack():
        ack_reader = asyncio.StreamReader()
        ack_reader.feed_data(frame_message(MsgType.CONFIG_ACK, 1, ack))
        ack_reader.feed_eof()
        await bridge._message_loop(bridge._sessions["session-a"], ack_reader)
    asyncio.run(process_ack())
    with database.session() as repo:
        operation = repo.get_admin_config_operation(response["operation_id"])
    assert operation["status"] == expected
    history_status, _, history = request(app, "/api/v1/config-operations?agent_id=agent-a")
    assert history_status == "200 OK"
    persisted = next(row for row in history["operations"] if row["operation_id"] == response["operation_id"])
    assert persisted["status"] == expected
