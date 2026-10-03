"""Tag UI/API/inspection tests; all OPC objects are simulated, never industrial COM."""
from __future__ import annotations

import asyncio
import functools
import io
import json
from pathlib import Path
import pickle
import uuid

import pytest

from opc_bridge.adapters.da import DevelopmentComItems, OpcDaAdapter
from opc_bridge.adapters.da_worker import process_worker_command, worker_error_response
from opc_bridge.adapters.simulated import SimulatedOpcAdapter
from opc_bridge.adapters.supervised import OpcWorkerConnectionError, SupervisedOpcAdapter
from opc_bridge.agent.client import AgentClient
from opc_bridge.agent.inspection import inspect_opc
from opc_bridge.protocol import ConfigPushPayload, Header, ItemRef, MsgType
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
    assert "allow 10.247.87.39;" in content
    assert "deny all;" in content
    assert "include /etc/opc-bridge/nginx-api-token.conf;" in content
    assert "limit_except GET POST HEAD" in content
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
        {"opc_item_path": "Good.Tag", "status": "valid", "hresult": None},
        {"opc_item_path": "Missing.Tag", "status": "invalid", "hresult": 0xC0040007}]
    assert "DO-NOT-EXPOSE" not in json.dumps(result)
    assert bridge._sessions["connected"].writer.frames == []
    assert active._groups == previous and active._connected
    assert all("read_device" not in candidate.commands for candidate in created)
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
    assert response["results"] == [{"opc_item_path": "Good.Tag", "status": "error", "hresult": 0x80040154}]
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
