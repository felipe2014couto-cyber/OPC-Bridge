"""Tag UI/API/inspection tests; all OPC objects are simulated, never industrial COM."""
from __future__ import annotations

import asyncio
import functools
import io
import json
from pathlib import Path
import pickle
import re
import struct
import time
import uuid

import pytest

from opc_bridge.adapters.da import DevelopmentComItems, OpcDaAdapter
from opc_bridge.adapters.da_worker import process_worker_command, worker_error_response
from opc_bridge.adapters.simulated import SimulatedOpcAdapter
from opc_bridge.adapters.supervised import OpcWorkerConnectionError, SupervisedOpcAdapter
from opc_bridge.agent.client import AgentClient
from opc_bridge.agent.inspection import inspect_opc
from opc_bridge.protocol import (
    ConfigPushPayload,
    Header,
    ItemRef,
    ItemResult,
    ItemStatus,
    MsgType,
    ReadResponsePayload,
    ValueType,
    decode_value,
)
from opc_bridge.protocol.inspection import CAPABILITY, InspectionRequest, InspectionResponse
from opc_bridge.server.admin import create_app
from opc_bridge.server.core import AgentSession, BridgeServer, ServerConfig
from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database
from tests.test_admin_api import ADMIN_TOKEN, request
from tests.test_worker_ipc import NonPicklableComGroup, NonPicklableComServer, NonPicklableGroups

PROG_ID = "ABB.AfwOpcDaSurrogate.1"
ROOT_DIR = Path(__file__).resolve().parent.parent


def async_test(coro_fn):
    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


class ComFailure(Exception):
    hresult = -1073479673  # OPC_E_UNKNOWNITEMID (0xC0040007).


class InspectionItems(DevelopmentComItems):
    fail_all = False

    def AddItem(self, path, handle):
        if path == "Missing.Tag" or self.fail_all:
            raise ComFailure("private provider diagnostic /secret/path token=DO-NOT-EXPOSE")
        return super().AddItem(path, handle)


class InspectionGroup(NonPicklableComGroup):
    def __init__(self, name):
        super().__init__(name)
        self.OPCItems = InspectionItems()


class InspectionGroups(NonPicklableGroups):
    def Add(self, name):
        group = InspectionGroup(name)
        self._groups[name] = group
        return group


class InspectionServer(NonPicklableComServer):
    def __init__(self):
        super().__init__()
        self.OPCGroups = InspectionGroups()


class SerializedInspectionAdapter(SupervisedOpcAdapter):
    """Production supervisor methods with a pickle boundary and fake COM worker."""

    def __init__(self):
        super().__init__()
        self.worker = OpcDaAdapter(com_factory=InspectionServer)
        self.commands = []
        self._conn = object()

    @property
    def is_alive(self):
        return self._conn is not None

    def _start_worker(self):
        self._conn = object()

    def _cleanup_process(self):
        self._conn = None

    def _send_raw(self, message, timeout=10):
        message = pickle.loads(pickle.dumps(message))
        self.commands.append(message["op"])
        try:
            if message["op"] == "discover_servers":
                response = {"ok": True, "servers": [PROG_ID, "Other.OPC"]}
            else:
                response = process_worker_command(self.worker, message)
        except Exception as exc:  # noqa: BLE001 - mirror the worker exception boundary.
            response = worker_error_response(exc)
        return pickle.loads(pickle.dumps(response))


class Writer:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)

    async def drain(self):
        pass


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(InspectionItems, "fail_all", False)
    database = sqlite_for_tests(str(tmp_path / "ui.sqlite"))
    upgrade_database(database)
    with database.session() as repo:
        repo.add_agent("agent-a", "Agent A")
        repo.add_credential("agent-a", "credential", "SECRET-HASH")
        repo.add_snapshot("agent-a", "active", 1, json.dumps({
            "config_version": 1, "update_rate_ms": 5000, "opc_prog_id": PROG_ID,
            "items": [{"item_id": 1, "opc_item_path": "Active.Tag", "requested_source": 0}],
            "auth_token": "PRIVATE-TOKEN", "certificate": "PRIVATE-CERT", "credential_hash": "SECRET-HASH",
        }))
        repo.add_operation("agent-a", "active-op", "active")
        repo.complete_operation("active-op", "applied")
    bridge = BridgeServer(ServerConfig(persistence=database))
    with database.session() as repo:
        repo.add_session("agent-a", "connected", "simulated-host", "simulated-OS", 1)
    writer = Writer()
    session = AgentSession("connected", writer, "agent-a", "host", "os", [CAPABILITY], config_version=1)
    bridge._sessions[session.session_id] = session
    app = create_app(database, bridge)
    created = []
    active = SimulatedOpcAdapter()
    active.connect("Simulated.OPC")

    def factory():
        adapter = SerializedInspectionAdapter()
        created.append(adapter)
        return adapter

    def inspect(agent_id, inspection):
        return inspect_opc(inspection, factory, active)

    monkeypatch.setattr(bridge, "inspect_agent_threadsafe", inspect)
    return app, bridge, database, created, active


def plan(tags=None, rate=5000):
    return {"opc_prog_id": PROG_ID, "update_rate_ms": rate, "tags": tags or ["Good.Tag"]}


def validate(app, body=None):
    return request(app, "/api/v1/agents/agent-a/tag-validations", method="POST", payload=body or plan())


def apply(app, approval, body=None, confirmed=True):
    data = dict(body or plan(), validation_id=approval, confirmed=confirmed)
    return request(app, "/api/v1/agents/agent-a/tag-config-operations", method="POST", payload=data)


def asset(app, path, token=None):
    statuses, headers = [], []

    def start(status, fields):
        statuses.append(status)
        headers.append(dict(fields))

    environ = {"PATH_INFO": path, "REQUEST_METHOD": "GET", "wsgi.input": io.BytesIO()}
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = "Bearer " + token
    body = b"".join(app(environ, start))
    return statuses[0], headers[0], body.decode()


def test_ui_authentication_and_static_page(runtime):
    app, _, _, _, _ = runtime
    for token in (None, "wrong"):
        status, headers, page = asset(app, "/ui", token)
        assert status.startswith("401")
        assert "login-form" not in page and "password" not in page
        assert "PRIVATE" not in page and "SECRET-HASH" not in page
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    status, _, page = asset(app, "/ui", ADMIN_TOKEN)
    assert status.startswith("200") and "Escrita OPC indisponível" in page
    assert "servers" in page and "prog-id" in page
    status, _, script = asset(app, "/ui/app.js")
    assert status.startswith("200")
    assert "window.confirm" in script and "confirmed: true" in script
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "Authorization" not in script and "Bearer" not in script


def test_ui_has_no_token_prompts_and_no_authorization_headers(runtime):
    app, _, _, _, _ = runtime
    status, _, page = asset(app, "/ui", ADMIN_TOKEN)
    assert status.startswith("200")
    assert "<form id=\"login-form\">" not in page
    assert "id=\"token\"" not in page
    assert "type=\"password\"" not in page
    assert "id=\"logout\"" not in page
    assert "id=\"workspace\"" in page
    assert "hidden" not in page.split("id=\"workspace\"", 1)[0].split("<div")[-1]

    status, _, script = asset(app, "/ui/app.js")
    assert status.startswith("200")
    assert "Authorization" not in script
    assert "Bearer" not in script
    assert "ADMIN_API_TOKEN" not in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "/api/v1/agents" in script
    assert "/api/v1/ui-capabilities" in script
    assert "window.confirm" in script


def test_nginx_template_configuration():
    nginx_conf = ROOT_DIR / "deploy" / "nginx" / "opc-bridge-admin-ui.conf.example"
    assert nginx_conf.is_file(), f"Nginx template not found at {nginx_conf}"
    content = nginx_conf.read_text(encoding="utf-8")

    assert "listen 10.247.168.43:8081 ssl;" in content
    assert "proxy_pass http://127.0.0.1:8081;" in content
    assert "auth_basic " in content
    assert "auth_basic_user_file /etc/opc-bridge/nginx-admin.htpasswd;" in content
    assert "allow 10.247.87.39;" not in content
    assert "include /etc/opc-bridge/nginx-api-token.conf;" in content
    assert "limit_except GET POST" in content
    assert "autoindex off;" in content
    assert "/etc/opc-bridge/tls/server/server.crt" in content
    assert "/etc/opc-bridge/tls/server/server.key" in content
    assert "<VALOR_DO_ADMIN_API_TOKEN" in content or "ADMIN_API_TOKEN" not in content


def test_opc_write_remains_strictly_unavailable(runtime):
    assert all("WRITE" not in msg.name for msg in MsgType)
    status, _, body = request(runtime[0], "/api/v1/agents/agent-a/write", method="POST", payload={"value": 123})
    assert status.startswith(("404", "405"))
    status, _, page = asset(runtime[0], "/ui", ADMIN_TOKEN)
    assert "Escrita OPC indisponível" in page
    assert "Nenhuma integração PI Point" in page


@pytest.mark.parametrize("endpoint,method", [("tag-validations", "POST"),
    ("tag-config-operations", "POST"), ("active-config", "GET"), ("opc-servers", "GET")])
@pytest.mark.parametrize("token", [None, "wrong"])
def test_api_authentication(runtime, endpoint, method, token):
    status, _, _ = request(runtime[0], "/api/v1/agents/agent-a/" + endpoint, token,
                           method=method, payload=plan() if method == "POST" else None)
    assert status.startswith("401")


def test_missing_admin_token_refuses_ui(runtime, monkeypatch):
    monkeypatch.delenv("ADMIN_API_TOKEN")
    with pytest.raises(RuntimeError, match="ADMIN_API_TOKEN"):
        create_app(runtime[2], runtime[1])


@pytest.mark.parametrize("rate", [999, 60001, 0, -1, True, 1000.5, "5000"])
def test_interval_limits(runtime, rate):
    assert validate(runtime[0], plan(rate=rate))[0].startswith("400")


@pytest.mark.parametrize("rate", [1000, 60000])
def test_interval_boundaries(runtime, rate):
    assert validate(runtime[0], plan(rate=rate))[2]["valid"] is True


@pytest.mark.parametrize("tags", [[], ["Tag"] * 2, [""], [None], [" Tag"], ["\ud800"],
    ["T" + str(i) for i in range(51)]])
def test_tag_limits(runtime, tags):
    data = plan()
    data["tags"] = tags
    assert validate(runtime[0], data)[0].startswith("400")


def test_maximum_tags(runtime):
    status, _, result = validate(runtime[0], plan(["T" + str(i) for i in range(50)]))
    assert status.startswith("200") and len(result["results"]) == 50


@pytest.mark.parametrize("raw", [b"{", b'null', b'{"opc_prog_id":"A","opc_prog_id":"B"}',
    b'{"tags":["T"],"opc_prog_id":"A","update_rate_ms":5000,"write":true}'])
def test_malformed_payloads(runtime, raw):
    status, _, _ = request(runtime[0], "/api/v1/agents/agent-a/tag-validations", method="POST", raw_body=raw)
    assert status.startswith("400")


def test_active_configuration_is_sanitized(runtime):
    status, _, result = request(runtime[0], "/api/v1/agents/agent-a/active-config")
    assert status.startswith("200")
    assert result["configuration"] == {"version": 1, "opc_prog_id": PROG_ID,
                                        "update_rate_ms": 5000, "tags": ["Active.Tag"]}
    assert "PRIVATE" not in json.dumps(result) and "SECRET-HASH" not in json.dumps(result)


def test_validation_non_destructive_with_nonpicklable_com(runtime):
    app, bridge, database, created, active = runtime
    previous = active._groups.copy()
    status, _, result = validate(app, plan(["Good.Tag", "Missing.Tag"]))
    assert status.startswith("200") and result["valid"] is False
    assert result["validation_id"] is None
    assert result["results"] == [
        {
            "opc_item_path": "Good.Tag",
            "status": "valid",
            "hresult": None,
            "value": 100.5,
            "value_type": "F64",
            "quality": 192,
            "quality_text": "Good",
            "opc_timestamp": result["results"][0]["opc_timestamp"],
            "error": None,
        },
        {
            "opc_item_path": "Missing.Tag",
            "status": "invalid",
            "hresult": 0xC0040007,
            "value": None,
            "value_type": None,
            "quality": None,
            "quality_text": None,
            "opc_timestamp": None,
            "error": "Endereço OPC não encontrado.",
        },
    ]
    assert "DO-NOT-EXPOSE" not in json.dumps(result)
    assert bridge._sessions["connected"].writer.frames == []
    assert active._groups == previous and active._connected
    assert any("read_device" in candidate.commands for candidate in created)
    assert created[0].commands[-2:] == ["disconnect", "exit"]
    assert created[0].startup_timeout == 10
    with database.session() as repo:
        assert len(repo.list_admin_config_operations("agent-a")) == 1
        assert repo.latest_applied_snapshot("agent-a").version == 1
        audits = repo._execute("SELECT detail_json FROM audit_events").fetchall()
    assert audits and "DO-NOT-EXPOSE" not in str(audits)


def test_select_opc_server(runtime):
    status, _, result = request(runtime[0], "/api/v1/agents/agent-a/opc-servers")
    assert status.startswith("200") and result == {"servers": [PROG_ID, "Other.OPC"]}
    assert runtime[1]._sessions["connected"].writer.frames == []


def test_disconnected_and_unknown_agent(runtime):
    app, bridge, _, _, _ = runtime
    assert request(app, "/api/v1/agents/missing/active-config")[0].startswith("404")
    bridge._sessions.clear()
    assert validate(app)[2]["error"] == "agent_disconnected"


def test_read_only_runtime_has_no_apply_or_validation(runtime):
    app = create_app(runtime[2])
    assert request(app, "/api/v1/ui-capabilities")[2]["dispatch_available"] is False
    assert validate(app)[0].startswith("405")
    assert request(app, "/api/v1/agents/agent-a/opc-servers")[0].startswith("405")


def test_apply_requires_confirmation_and_current_matching_validation(runtime, monkeypatch):
    app, bridge, database, _, _ = runtime
    dispatched = []
    monkeypatch.setattr(bridge, "dispatch_admin_config_operation_threadsafe",
                        lambda agent, operation, payload, **kwargs: dispatched.append(payload))
    approval = validate(app)[2]["validation_id"]
    assert apply(app, approval, confirmed=False)[2]["error"] == "confirmation_required"
    assert dispatched == []
    assert apply(app, approval, plan(["Changed.Tag"]))[2]["error"] == "validation_required"
    approval = validate(app)[2]["validation_id"]
    status, _, operation = apply(app, approval)
    assert status.startswith("201") and operation["status"] == "pending"
    assert len(dispatched) == 1
    assert set(operation) == {"operation_id", "agent_id", "version", "status"}
    assert apply(app, approval)[2]["error"] == "validation_required"
    with database.session() as repo:
        assert repo.latest_applied_snapshot("agent-a").version == 1


def test_deterministic_backend_ids(runtime, monkeypatch):
    app, bridge, _, _, _ = runtime
    dispatched = []
    monkeypatch.setattr(bridge, "dispatch_admin_config_operation_threadsafe",
                        lambda agent, operation, payload, **kwargs: dispatched.append(payload))
    for tags in (["B.Tag", "A.Tag"], ["A.Tag", "B.Tag"]):
        body = plan(tags)
        ticket = validate(app, body)[2]["validation_id"]
        assert apply(app, ticket, body)[0].startswith("201")
    assert dispatched[0].items == dispatched[1].items == [ItemRef(1, "A.Tag"), ItemRef(2, "B.Tag")]
    assert dispatched[1].config_version == dispatched[0].config_version + 1


def test_expired_or_changed_session_validation(runtime, monkeypatch):
    app, bridge, _, _, _ = runtime
    ticket = validate(app)[2]["validation_id"]
    old = app._validation_tickets[ticket]
    app._validation_tickets[ticket] = (*old[:3], 0)
    assert apply(app, ticket)[2]["error"] == "validation_required"
    ticket = validate(app)[2]["validation_id"]
    bridge._sessions["connected"].config_version += 1
    assert apply(app, ticket)[2]["error"] == "validation_required"


def test_no_write_endpoints_or_protocol(runtime):
    assert all("WRITE" not in msg.name for msg in MsgType)
    status, _, _ = request(runtime[0], "/api/v1/agents/agent-a/write", method="POST", payload={})
    assert status.startswith(("405", "404"))


def test_dispatch_conflict_is_explicit_and_audited(runtime, monkeypatch):
    app, bridge, database, _, _ = runtime

    def conflict(agent, operation, payload, expected_context):
        assert expected_context == ("connected", 1)
        return False

    monkeypatch.setattr(bridge, "dispatch_admin_config_operation_threadsafe", conflict)
    ticket = validate(app)[2]["validation_id"]
    status, _, body = apply(app, ticket)
    assert status.startswith("409") and body["error"] == "configuration_conflict"
    with database.session() as repo:
        assert any(row["status"] == "failed" for row in repo.list_admin_config_operations("agent-a"))
        assert repo.latest_applied_snapshot("agent-a").version == 1


@async_test
async def test_dispatch_rechecks_session_context_in_event_loop(runtime):
    bridge = runtime[1]
    session = bridge._sessions["connected"]
    payload = ConfigPushPayload(2, 5000, [ItemRef(1, "Good.Tag")], PROG_ID)
    session.config_version = 2
    assert await bridge.dispatch_admin_config_operation(
        "agent-a", "unpersisted-test", payload, expected_context=("connected", 1)) is False
    assert session.writer.frames == []


@async_test
async def test_ui_operation_expiry_and_safe_status_query(runtime):
    app, bridge, _, _, _ = runtime
    bridge._event_loop = asyncio.get_running_loop()
    bridge.config.config_ack_timeout_ms = 1
    ticket = validate(app)[2]["validation_id"]
    status, _, operation = await asyncio.get_running_loop().run_in_executor(None, apply, app, ticket)
    assert status.startswith("201")
    await asyncio.sleep(.02)
    status, _, response = request(app, "/api/v1/config-operations/" + operation["operation_id"])
    assert status.startswith("200") and response["operation"]["status"] == "expired"
    assert bridge._sessions["connected"].config_version == 1
    assert request(app, "/api/v1/config-operations/missing")[0].startswith("404")


def test_inspection_cannot_use_active_adapter():
    active = SimulatedOpcAdapter()
    active.connect("Simulated.OPC")
    result = inspect_opc(InspectionRequest(str(uuid.uuid4()), "tags", PROG_ID, ["Good.Tag"]),
                         lambda: active, active)
    assert result.error == "unavailable" and active._connected


def test_prog_id_failure_has_only_safe_per_tag_hresult(runtime, monkeypatch):
    app, bridge, _, _, _ = runtime

    class FailedConnection(SerializedInspectionAdapter):
        def connect(self, prog_id):
            raise OpcWorkerConnectionError("PRIVATE connection certificate=/secret", 0x80040154)

    monkeypatch.setattr(bridge, "inspect_agent_threadsafe", lambda agent, req:
                        inspect_opc(req, FailedConnection, SimulatedOpcAdapter()))
    status, _, response = validate(app)
    assert status.startswith("200") and response["valid"] is False
    assert response["results"] == [{
        "opc_item_path": "Good.Tag",
        "status": "error",
        "hresult": 0x80040154,
        "value": None,
        "value_type": None,
        "quality": None,
        "quality_text": None,
        "opc_timestamp": None,
        "error": "0x80040154",
    }]
    assert "PRIVATE" not in json.dumps(response)


def test_inspection_timeout_is_safe_and_cannot_approve(runtime, monkeypatch):
    def timeout(agent, req):
        raise TimeoutError("PRIVATE socket path /secret")

    monkeypatch.setattr(runtime[1], "inspect_agent_threadsafe", timeout)
    status, _, response = validate(runtime[0])
    assert status.startswith("504") and response == {"error": "inspection_timeout"}
    assert runtime[0]._validation_tickets == {}


def test_validation_ticket_isolated_by_agent(runtime):
    app, bridge, database, _, _ = runtime
    with database.session() as repo:
        repo.add_agent("agent-b", "Agent B")
    bridge._sessions["other-session"] = AgentSession("other-session", Writer(), "agent-b", "host", "os",
                                                   [CAPABILITY], config_version=1)
    ticket = validate(app)[2]["validation_id"]
    status, _, response = request(app, "/api/v1/agents/agent-b/tag-config-operations", method="POST",
                                  payload=dict(plan(), validation_id=ticket, confirmed=True))
    assert status.startswith("409") and response["error"] == "validation_required"
    with database.session() as repo:
        assert repo.list_admin_config_operations("agent-b") == []


@async_test
async def test_inspection_end_to_end_messages_never_send_config_push(runtime, monkeypatch):
    _, bridge, _, _, _ = runtime
    session = bridge._sessions["connected"]
    active = SimulatedOpcAdapter()
    active.connect("Simulated.OPC")
    client = AgentClient("localhost", 1, "agent-a", b"x" * 32, active,
                         adapter_factory=SerializedInspectionAdapter)
    incoming = asyncio.Queue()
    kinds = []

    async def client_send(kind, payload):
        kinds.append(kind)
        await incoming.put((Header(msg_type=kind), payload))

    async def session_send(kind, payload):
        kinds.append(kind)
        await client._handle_inspection_request(payload)

    async def read_message(reader):
        return await incoming.get()

    client._send = client_send
    session.send = session_send
    monkeypatch.setattr(bridge, "_read_message", read_message)
    bridge._running = True
    processor = asyncio.create_task(bridge._message_loop(session, None))
    try:
        response = await bridge.inspect_agent("agent-a", InspectionRequest(
            str(uuid.uuid4()), "tags", PROG_ID, ["Good.Tag"]))
        assert response.results[0]["status"] == "valid"
        assert kinds == [MsgType.OPC_INSPECT_REQUEST, MsgType.OPC_INSPECT_RESPONSE]
        assert client._adapter is active and client.config_version == 0
        assert bridge._pending_inspections == {} and session.config_version == 1
    finally:
        await incoming.put(None)
        await processor
        bridge._running = False


@async_test
async def test_inspection_timeout_cleans_pending_future(runtime, monkeypatch):
    bridge = runtime[1]
    wait_for = asyncio.wait_for

    async def short_wait(future, timeout):
        return await wait_for(future, .001)

    monkeypatch.setattr(asyncio, "wait_for", short_wait)
    with pytest.raises(asyncio.TimeoutError):
        await bridge.inspect_agent("agent-a", InspectionRequest(
            str(uuid.uuid4()), "tags", PROG_ID, ["Good.Tag"]))
    assert bridge._pending_inspections == {}
    assert bridge._pending_config_operations == {}


def test_inspection_timeout_does_not_retry_more_tags():
    adapters = []

    class TimeoutAdapter(SerializedInspectionAdapter):
        def add_items(self, group, paths):
            self.commands.append("timeout_add")
            raise TimeoutError("unsafe detail")

    def factory():
        candidate = TimeoutAdapter()
        adapters.append(candidate)
        return candidate

    result = inspect_opc(InspectionRequest(str(uuid.uuid4()), "tags", PROG_ID, ["A", "B"]),
                         factory, SimulatedOpcAdapter())
    assert all(r["status"] == "error" for r in result.results)
    assert adapters[0].commands.count("timeout_add") == 1


def test_temporary_worker_startup_deadline_reaps_failed_child(monkeypatch):
    from types import SimpleNamespace

    from opc_bridge.adapters import supervised

    class Socket:
        timeout = None

        def fileno(self):
            return 1

        def settimeout(self, seconds):
            self.timeout = seconds

    socket = Socket()

    class Listener:
        address = ("127.0.0.1", 9999)
        _listener = SimpleNamespace(_socket=socket)
        closed = False

        def accept(self):
            raise TimeoutError("test-only child did not connect")

        def close(self):
            self.closed = True

    class Process:
        pid = 123
        killed = False
        reaped = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            self.reaped = True

    listener, child = Listener(), Process()
    monkeypatch.setattr(supervised, "Listener", lambda *args, **kwargs: listener)
    monkeypatch.setattr(supervised.subprocess, "Popen", lambda *args, **kwargs: child)
    adapter = SupervisedOpcAdapter(startup_timeout=10)
    with pytest.raises(ConnectionError):
        adapter._start_worker()
    assert socket.timeout == 10 and child.killed and child.reaped and listener.closed
    assert adapter._process is None and adapter._conn is None


@async_test
async def test_agent_inspection_message_does_not_apply_configuration():
    active = SimulatedOpcAdapter()
    active.connect("Simulated.OPC")
    client = AgentClient("localhost", 1, "test", b"x" * 32, active,
                         adapter_factory=SerializedInspectionAdapter)
    sent = []

    async def send(kind, payload):
        sent.append((kind, payload))

    client._send = send
    inspection = InspectionRequest(str(uuid.uuid4()), "tags", PROG_ID, ["Good.Tag"])
    await client._handle_inspection_request(inspection.pack())
    await client._inspection_task
    assert len(sent) == 1 and sent[0][0] == MsgType.OPC_INSPECT_RESPONSE
    assert InspectionResponse.unpack(sent[0][1]).results[0]["status"] == "valid"
    assert client._adapter is active and client.config_version == 0


@async_test
async def test_bridge_inspection_capability_and_correlation(runtime):
    _, bridge, _, _, _ = runtime
    session = bridge._sessions["connected"]
    request_payload = InspectionRequest(str(uuid.uuid4()), "tags", PROG_ID, ["Good.Tag"])
    session.capabilities = []
    with pytest.raises(ValueError, match="inspection_unsupported"):
        await bridge.inspect_agent("agent-a", request_payload)
    assert session.writer.frames == []
    session.capabilities = [CAPABILITY]

    async def send(kind, payload):
        assert kind == MsgType.OPC_INSPECT_REQUEST
        incoming = InspectionRequest.unpack(payload)
        response = inspect_opc(incoming, SerializedInspectionAdapter, SimulatedOpcAdapter())
        future = bridge._pending_inspections[(session.session_id, incoming.request_id)]
        future.set_result(InspectionResponse.unpack(response.pack()))

    session.send = send
    response = await bridge.inspect_agent("agent-a", request_payload)
    assert response.results[0]["status"] == "valid"
    assert bridge._pending_inspections == {} and session.config_version == 1


@async_test
async def test_confirmed_ui_apply_ack_and_atomic_rejection(runtime, monkeypatch):
    app, bridge, database, _, _ = runtime
    session = bridge._sessions["connected"]
    client = AgentClient("localhost", 1, "agent-a", b"x" * 32, SimulatedOpcAdapter(),
                         adapter_factory=SerializedInspectionAdapter)
    incoming = asyncio.Queue()
    applied = asyncio.Event()

    async def client_send(kind, payload):
        await incoming.put((Header(msg_type=kind), payload))
        applied.set()

    client._send = client_send

    async def session_send(kind, payload):
        assert kind == MsgType.CONFIG_PUSH
        await client._handle_config_push(payload)

    async def read_message(reader):
        return await incoming.get()

    session.send = session_send
    monkeypatch.setattr(bridge, "_read_message", read_message)
    bridge._running = True
    bridge._event_loop = asyncio.get_running_loop()
    processor = asyncio.create_task(bridge._message_loop(session, None))
    try:
        ticket = validate(app)[2]["validation_id"]
        result = await asyncio.get_running_loop().run_in_executor(None, apply, app, ticket)
        await applied.wait()
        await asyncio.sleep(0)
        assert result[0].startswith("201")
        first_version = client.config_version
        old_adapter = client._adapter
        with database.session() as repo:
            assert repo.latest_applied_snapshot("agent-a").version == first_version
        applied.clear()
        ticket = validate(app)[2]["validation_id"]
        monkeypatch.setattr(InspectionItems, "fail_all", True)
        result = await asyncio.get_running_loop().run_in_executor(None, apply, app, ticket)
        await applied.wait()
        await asyncio.sleep(0)
        assert result[0].startswith("201")
        assert client._adapter is old_adapter and client.config_version == first_version
        history = request(app, "/api/v1/config-operations?agent_id=agent-a")[2]["operations"]
        assert any(o["status"] == "rejected" for o in history)
        assert any(o["status"] == "applied" and o["version"] == first_version for o in history)
        with database.session() as repo:
            assert repo.latest_applied_snapshot("agent-a").version == first_version
    finally:
        await incoming.put(None)
        await processor
        bridge._running = False


@pytest.mark.parametrize("raw", [b"{}", b"[]", b'{"request_id":null}', pytest.param(b"x" * 131073, id="oversized_payload")])
def test_protocol_rejects_invalid_inspection(raw):
    with pytest.raises((ValueError, TypeError)):
        InspectionRequest.unpack(raw)
    with pytest.raises((ValueError, TypeError)):
        InspectionResponse.unpack(raw)


def test_decode_value_and_types():
    assert decode_value(ValueType.I16, struct.pack("<h", 1234))[0] == 1234
    assert decode_value(ValueType.I32, struct.pack("<i", 123456))[0] == 123456
    assert abs(decode_value(ValueType.F32, struct.pack("<f", 12.5))[0] - 12.5) < 1e-4
    assert abs(decode_value(ValueType.F64, struct.pack("<d", 98.765))[0] - 98.765) < 1e-5
    assert decode_value(ValueType.BOOL, struct.pack("<?", True))[0] is True
    assert decode_value(ValueType.BOOL, struct.pack("<?", False))[0] is False
    assert decode_value(ValueType.STRING, "Hello OPC".encode("utf-8"))[0] == "Hello OPC"
    assert decode_value(ValueType.BLOB, b"\x01\x02")[0] == "0102"
    assert decode_value(ValueType.F64, b"")[0] is None
    assert decode_value(ValueType.I32, b"\x01")[0] is None  # too short


def test_live_values_unauthorized(runtime):
    app, _, _, _, _ = runtime
    status, _, data = request(app, "/api/v1/agents/agent-a/live-values", token="")
    assert status.startswith("401")
    assert data["error"] == "unauthorized"


def test_live_values_unknown_agent(runtime):
    app, _, _, _, _ = runtime
    status, _, data = request(app, "/api/v1/agents/nonexistent/live-values")
    assert status.startswith("404")
    assert data["error"] == "not_found"


def test_live_values_agent_disabled(runtime):
    app, _, database, _, _ = runtime
    with database.session() as repo:
        repo.add_agent("disabled-agent", "Disabled Agent")
        repo._execute("UPDATE agents SET enabled = 0 WHERE agent_id = ?", ("disabled-agent",))
    status, _, data = request(app, "/api/v1/agents/disabled-agent/live-values")
    assert status.startswith("409")
    assert data["error"] == "agent_disabled"


def test_live_values_agent_disconnected(runtime):
    app, bridge, database, _, _ = runtime
    with database.session() as repo:
        repo.add_agent("disconnected-agent", "Disconnected Agent")
    status, _, data = request(app, "/api/v1/agents/disconnected-agent/live-values")
    assert status.startswith("200")
    assert data["connected"] is False
    assert data["status"] == "agent_disconnected"
    assert data["items"] == []


def test_live_values_waiting_first_read(runtime):
    app, bridge, _, _, _ = runtime
    session = bridge._sessions["connected"]
    session.config_version = 1
    session.config_items = [
        ItemRef(item_id=1, opc_item_path="Tag.A"),
        ItemRef(item_id=2, opc_item_path="Tag.B"),
    ]
    status, _, data = request(app, "/api/v1/agents/agent-a/live-values")
    assert status.startswith("200")
    assert data["connected"] is True
    assert data["config_version"] == 1
    assert data["status"] == "ok"
    assert len(data["items"]) == 2
    for it in data["items"]:
        assert it["available"] is False
        assert it["status"] == "waiting_first_read"
        assert it["value"] is None
        assert it["stale"] is False
        assert it["error"] is None


def test_live_values_simulated_read_response_and_types(runtime):
    app, bridge, _, _, _ = runtime
    session = bridge._sessions["connected"]
    session.config_version = 1
    session.config_items = [
        ItemRef(item_id=1, opc_item_path="Tag.Pressure"),
        ItemRef(item_id=2, opc_item_path="Tag.Count"),
        ItemRef(item_id=3, opc_item_path="Tag.Running"),
        ItemRef(item_id=4, opc_item_path="Tag.Faulty"),
    ]
    now_us = int(time.time() * 1_000_000)
    resp = ReadResponsePayload(
        request_id=101,
        duration_us=1500,
        results=[
            ItemResult(
                item_id=1,
                status=ItemStatus.OK,
                value_type=ValueType.F64,
                quality=192,
                timestamp_us=now_us,
                value=struct.pack("<d", 42.5),
                error_code=0,
            ),
            ItemResult(
                item_id=2,
                status=ItemStatus.OK,
                value_type=ValueType.I32,
                quality=192,
                timestamp_us=now_us,
                value=struct.pack("<i", 100),
                error_code=0,
            ),
            ItemResult(
                item_id=3,
                status=ItemStatus.OK,
                value_type=ValueType.BOOL,
                quality=192,
                timestamp_us=now_us,
                value=struct.pack("<?", True),
                error_code=0,
            ),
            ItemResult(
                item_id=4,
                status=ItemStatus.ERROR,
                value_type=ValueType.STRING,
                quality=0,
                timestamp_us=now_us,
                value=b"",
                error_code=0xC0040007,  # OPC_E_UNKNOWNITEMID
            ),
        ],
    )
    bridge._record_live_values(session, resp)
    status, _, data = request(app, "/api/v1/agents/agent-a/live-values")
    assert status.startswith("200")
    items = {it["opc_item_path"]: it for it in data["items"]}

    # Tag.Pressure
    p = items["Tag.Pressure"]
    assert p["available"] is True
    assert p["status"] == "ok"
    assert p["value"] == 42.5
    assert p["value_type"] == "F64"
    assert p["quality"] == 192
    assert p["quality_text"] == "Good"
    assert p["stale"] is False
    assert p["error"] is None
    assert p["age_ms"] is not None and p["age_ms"] >= 0

    # Tag.Count
    c = items["Tag.Count"]
    assert c["available"] is True
    assert c["value"] == 100
    assert c["value_type"] == "I32"

    # Tag.Running
    r = items["Tag.Running"]
    assert r["available"] is True
    assert r["value"] is True
    assert r["value_type"] == "BOOL"

    # Tag.Faulty (sanitized error)
    f = items["Tag.Faulty"]
    assert f["available"] is True
    assert f["status"] == "error"
    assert f["value"] is None
    assert f["quality"] == 0
    assert f["quality_text"] == "Bad"
    assert "OPC_E_UNKNOWNITEMID" in f["error"]
    assert "0xC0040007" in f["error"]


def test_live_values_filters_strictly_active_configuration(runtime):
    app, bridge, _, _, _ = runtime
    session = bridge._sessions["connected"]
    session.config_version = 1
    session.config_items = [
        ItemRef(item_id=1, opc_item_path="Active.Only"),
    ]
    resp = ReadResponsePayload(
        request_id=102,
        duration_us=1000,
        results=[
            ItemResult(item_id=1, status=ItemStatus.OK, value_type=ValueType.I32, quality=192,
                       timestamp_us=int(time.time() * 1_000_000), value=struct.pack("<i", 10), error_code=0),
            ItemResult(item_id=999, status=ItemStatus.OK, value_type=ValueType.I32, quality=192,
                       timestamp_us=int(time.time() * 1_000_000), value=struct.pack("<i", 999), error_code=0),
        ],
    )
    bridge._record_live_values(session, resp)
    status, _, data = request(app, "/api/v1/agents/agent-a/live-values")
    assert status.startswith("200")
    paths = [it["opc_item_path"] for it in data["items"]]
    assert paths == ["Active.Only"]
    assert 999 not in [it["item_id"] for it in data["items"]]


def test_live_values_stale_and_disconnect_cleanup(runtime, monkeypatch):
    app, bridge, _, _, _ = runtime
    session = bridge._sessions["connected"]
    session.config_version = 1
    session.update_rate_ms = 1000  # TTL = max(3*1000, 15000) = 15000 ms
    session.config_items = [ItemRef(item_id=1, opc_item_path="Tag.A")]

    now_wall = time.time()
    now_mono = time.monotonic()
    resp = ReadResponsePayload(
        request_id=103,
        duration_us=500,
        results=[
            ItemResult(item_id=1, status=ItemStatus.OK, value_type=ValueType.I32, quality=192,
                       timestamp_us=int(now_wall * 1_000_000), value=struct.pack("<i", 55), error_code=0),
        ],
    )
    bridge._record_live_values(session, resp)

    # Initially fresh
    status, _, data = request(app, "/api/v1/agents/agent-a/live-values")
    assert data["items"][0]["stale"] is False
    assert data["items"][0]["status"] == "ok"

    # Simulate 20 seconds later (exceeding TTL of 15s)
    monkeypatch.setattr(time, "monotonic", lambda: now_mono + 25.0)
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


def test_live_values_no_database_persistence_and_no_value_logging(runtime, caplog):
    app, bridge, database, _, _ = runtime
    session = bridge._sessions["connected"]
    session.config_version = 1
    session.config_items = [ItemRef(item_id=1, opc_item_path="Secret.Sensor.Tag")]

    secret_val = 987654321
    resp = ReadResponsePayload(
        request_id=104,
        duration_us=800,
        results=[
            ItemResult(item_id=1, status=ItemStatus.OK, value_type=ValueType.I32, quality=192,
                       timestamp_us=int(time.time() * 1_000_000), value=struct.pack("<i", secret_val), error_code=0),
        ],
    )

    caplog.clear()
    with caplog.at_level("DEBUG"):
        bridge._record_live_values(session, resp)
        status, _, data = request(app, "/api/v1/agents/agent-a/live-values")

    assert status.startswith("200")
    assert data["items"][0]["value"] == secret_val

    # Check database: NO process values must exist anywhere in SQLite
    with database.session() as repo:
        audit_rows = repo._execute("SELECT detail_json FROM audit_events").fetchall()
        for row in audit_rows:
            assert str(secret_val) not in str(row[0])
            assert "Secret.Sensor.Tag" not in str(row[0])
        snapshot_rows = repo._execute("SELECT payload_json FROM config_snapshots").fetchall()
        for row in snapshot_rows:
            assert str(secret_val) not in str(row[0])
            assert "Secret.Sensor.Tag" not in str(row[0])
        op_rows = repo._execute("SELECT status FROM config_operations").fetchall()
        for row in op_rows:
            assert str(secret_val) not in str(row[0])

    # Check logs: NO process values or tag names logged
    for record in caplog.records:
        assert str(secret_val) not in record.getMessage()
        assert "Secret.Sensor.Tag" not in record.getMessage()


def test_live_values_polling_does_not_trigger_opc_reads_or_config_push(runtime):
    app, bridge, _, _, _ = runtime
    session = bridge._sessions["connected"]
    session.config_version = 1
    session.config_items = [ItemRef(item_id=1, opc_item_path="Test.Tag")]
    writer = session.writer
    initial_frames_count = len(writer.frames)

    for _ in range(5):
        status, _, _ = request(app, "/api/v1/agents/agent-a/live-values")
        assert status.startswith("200")

    # Zero frames sent!
    assert len(writer.frames) == initial_frames_count


def test_opc_write_remains_strictly_unavailable(runtime):
    app, _, _, _, _ = runtime
    # POST to live-values must return 405 Method Not Allowed
    status, _, data = request(app, "/api/v1/agents/agent-a/live-values", method="POST", payload={"write": 123})
    assert status.startswith("405")
    assert data["error"] == "method_not_allowed"


def test_ui_assets_live_controls_and_security_hygiene(runtime):
    app, _, _, _, _ = runtime
    status, _, html = asset(app, "/ui", ADMIN_TOKEN)
    assert status.startswith("200")
    assert 'id="toggle-live"' in html
    assert ('Iniciar acompanhamento ao vivo' in html or 'Iniciar valores ao vivo' in html)
    assert 'id="live-values"' in html
    assert 'id="live-panel"' in html
    assert "Escrita OPC indisponível" in html

    status, _, js = asset(app, "/ui/app.js", ADMIN_TOKEN)
    assert status.startswith("200")
    assert "livePolling" in js
    assert "fetchLiveValues" in js
    assert "renderLiveTable" in js
    assert "stopLive" in js
    assert "visibilitychange" in js
    assert "pagehide" in js
    assert "beforeunload" in js

    # Hygiene: No secrets in JS or HTML
    assert "localStorage" not in js
    assert "sessionStorage" not in js
    assert "ADMIN_API_TOKEN" not in js
    assert "ADMIN_API_TOKEN" not in html


def test_snapshot_valid_returns_value_type_quality_and_timestamp(runtime):
    app, bridge, database, created, active = runtime
    status, _, result = validate(app, plan(["Good.Tag"]))
    assert status.startswith("200")
    assert result["valid"] is True
    assert result["validation_id"] is not None
    assert len(result["results"]) == 1
    tag_res = result["results"][0]
    assert tag_res["opc_item_path"] == "Good.Tag"
    assert tag_res["status"] == "valid"
    assert tag_res["hresult"] is None
    assert tag_res["value"] == 100.5
    assert tag_res["value_type"] == "F64"
    assert tag_res["quality"] == 192
    assert tag_res["quality_text"] == "Good"
    assert tag_res["opc_timestamp"] is not None
    assert re.match(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}\.\d{3}$", tag_res["opc_timestamp"])
    assert tag_res["error"] is None


def test_snapshot_mixed_valid_and_nonexistent_tags_without_interruption(runtime):
    app, bridge, database, created, active = runtime
    status, _, result = validate(app, plan(["Good.Tag.1", "Missing.Tag", "Good.Tag.2"]))
    assert status.startswith("200")
    assert result["valid"] is False
    assert result["validation_id"] is None
    assert len(result["results"]) == 3

    res_by_path = {r["opc_item_path"]: r for r in result["results"]}
    r1 = res_by_path["Good.Tag.1"]
    r2 = res_by_path["Missing.Tag"]
    r3 = res_by_path["Good.Tag.2"]

    assert r1["status"] == "valid"
    assert r1["value"] is not None
    assert r1["quality_text"] == "Good"
    assert r1["error"] is None

    assert r2["status"] == "invalid"
    assert r2["hresult"] == 0xC0040007
    assert r2["error"] == "Endereço OPC não encontrado."
    assert r2["value"] is None

    assert r3["status"] == "valid"
    assert r3["value"] is not None
    assert r3["quality_text"] == "Good"
    assert r3["error"] is None


def test_validation_error_highlight_and_consolidated_warning(runtime):
    app, _, _, _, _ = runtime
    status, _, html = asset(app, "/ui", ADMIN_TOKEN)
    assert status.startswith("200")
    assert 'id="validation-errors-banner"' in html
    assert 'Validar e ler agora' in html
    assert 'Validar lista' not in html

    status, _, css = asset(app, "/ui/style.css", ADMIN_TOKEN)
    assert status.startswith("200")
    assert '.banner.danger' in css
    assert 'tr.row-error td' in css
    assert '.col-qual.error' in css
    assert '.col-qual.valid' in css

    status, _, js = asset(app, "/ui/app.js", ADMIN_TOKEN)
    assert status.startswith("200")
    assert 'validation-errors-banner' in js
    assert 'Endereço OPC não encontrado.' in js
    assert 'row-error' in js
    assert 'clearValidationHighlights' in js
    assert 'endereços OPC não foram encontrados' in js
    assert 'endereço OPC não foi encontrado' in js


def test_absence_of_visual_history_block(runtime):
    app, _, _, _, _ = runtime
    status, _, html = asset(app, "/ui", ADMIN_TOKEN)
    assert status.startswith("200")
    assert "Histórico de aplicações de configuração" not in html
    assert "Configuração ativa no agente" in html
    assert "Carregar no formulário" in html


def test_snapshot_validation_does_not_send_config_push_nor_persist_nor_start_live_values(runtime):
    app, bridge, database, created, active = runtime
    session = bridge._sessions["connected"]
    initial_version = session.config_version
    initial_frames_count = len(session.writer.frames)

    with database.session() as repo:
        ops_before = repo.list_admin_config_operations("agent-a")
        snaps_before = len(repo._execute("SELECT snapshot_id FROM config_snapshots").fetchall())

    # Execute snapshot validation
    status, _, result = validate(app, plan(["Good.Tag", "Missing.Tag"]))
    assert status.startswith("200")

    # 1. No frames (especially no CONFIG_PUSH) sent to active agent session
    assert len(session.writer.frames) == initial_frames_count
    assert all(frame[4] != MsgType.CONFIG_PUSH for frame in session.writer.frames)

    # 2. Config version unchanged
    assert session.config_version == initial_version

    # 3. No operations or snapshots created in database
    with database.session() as repo:
        ops_after = repo.list_admin_config_operations("agent-a")
        snaps_after = len(repo._execute("SELECT snapshot_id FROM config_snapshots").fetchall())
    assert len(ops_after) == len(ops_before)
    assert snaps_after == snaps_before

    # 4. Live values cache is NOT populated or started
    live_data = bridge.get_live_values("agent-a")
    assert live_data["items"] == []
