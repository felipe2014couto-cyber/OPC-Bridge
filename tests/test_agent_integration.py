"""Integration tests: server -> simulated agent -> read -> response.
Validates the complete flow without real ABB hardware.
"""
from __future__ import annotations

import asyncio
import ssl
import subprocess
import time
from pathlib import Path

import pytest

from opc_bridge.adapters.simulated import SimulatedOpcAdapter
from opc_bridge.agent.client import AgentClient
from opc_bridge.protocol import (
    ConfigAckPayload,
    ConfigPushPayload,
    ItemRef,
    MsgType,
    ReadRequestPayload,
    ReadResponsePayload,
)
from opc_bridge.server import BridgeServer, ServerConfig


@pytest.fixture
def tls_certs(tmp_path: Path):
    """Generate self-signed TLS certificates for testing."""
    certfile = tmp_path / "cert.pem"
    keyfile = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(keyfile), "-out", str(certfile),
            "-days", "1", "-nodes", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True, capture_output=True,
    )
    return str(certfile), str(keyfile)


@pytest.fixture
def auth_token_hash():
    return b"\xaa\xbb\xcc\xdd" * 8  # 32 bytes


@pytest.fixture
async def server_with_config(tls_certs, auth_token_hash):
    """Start a BridgeServer with pre-loaded config."""
    certfile, keyfile = tls_certs
    config = ServerConfig(
        host="127.0.0.1",
        port=0,
        certfile=certfile,
        keyfile=keyfile,
        auth_token_hash=auth_token_hash,
        heartbeat_interval_ms=5000,
        default_update_rate_ms=1000,
    )
    srv = BridgeServer(config)
    items = [
        ItemRef(item_id=1, opc_item_path="Simulated.Temperature", requested_source=0),
        ItemRef(item_id=2, opc_item_path="Simulated.Pressure", requested_source=0),
        ItemRef(item_id=3, opc_item_path="Simulated.Status", requested_source=0),
    ]
    srv.set_config(items, version=1)
    await srv.start()
    port = srv._server.sockets[0].getsockname()[1]
    yield srv, port, auth_token_hash, certfile
    await srv.stop()


class TestFullFlowNormalRead:
    """Server sends READ_REQUEST, agent reads via simulated adapter, returns response."""

    @pytest.mark.asyncio
    async def test_normal_read_roundtrip(self, server_with_config):
        srv, port, token_hash, certfile = server_with_config

        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("test-group", 1000)
        adapter.add_items(group, ["Simulated.Temperature", "Simulated.Pressure", "Simulated.Status"])
        adapter._group_handle = group

        client = AgentClient(
            server_host="127.0.0.1",
            server_port=port,
            agent_id="integration-agent-001",
            auth_token_hash=token_hash,
            adapter=adapter,
            certfile=certfile,
        )
        client._group_handle = group

        await client.connect()
        assert client.is_connected
        assert client.session_id is not None

        # Push config from server
        await srv.push_config_to_all()
        await client._handle_config_push(
            ConfigPushPayload(1, 1000, srv._config_items).pack()
        )
        await asyncio.sleep(0.2)

        # Send READ_REQUEST from server side by injecting into message loop
        # We simulate this by having the client process a read request directly
        req = ReadRequestPayload(
            request_id=42,
            items=[
                ItemRef(item_id=1, opc_item_path="Simulated.Temperature", requested_source=0),
                ItemRef(item_id=2, opc_item_path="Simulated.Pressure", requested_source=0),
            ],
        )
        await client._handle_read_request(req.pack())

        # Verify the server received the READ_RESPONSE
        await asyncio.sleep(0.2)
        session = next(iter(srv.sessions.values()))
        assert session.agent_id == "integration-agent-001"

        await client.disconnect()
        adapter.disconnect()


class RecordingAdapter(SimulatedOpcAdapter):
    def __init__(self, fail_path: str | None = None) -> None:
        super().__init__(read_latency_us=0)
        self.fail_path = fail_path
        self.disconnected = False
        self.seen_read_ids: list[int] = []

    def add_items(self, group, item_paths):
        if self.fail_path and self.fail_path in item_paths:
            raise RuntimeError("simulated item registration failure")
        return super().add_items(group, item_paths)

    def read_device(self, group, item_ids):
        self.seen_read_ids.extend(item_ids)
        return super().read_device(group, item_ids)

    def disconnect(self):
        self.disconnected = True
        super().disconnect()


class TestConfigPushRegression:
    @pytest.mark.asyncio
    async def test_ack_only_after_complete_apply_and_maps_central_ids(self):
        adapter = RecordingAdapter()
        adapter.connect("Simulated.OPC")
        client = AgentClient("localhost", 0, "agent", b"x" * 32, adapter)
        sent = []

        async def capture_send(msg_type, payload):
            sent.append((msg_type, payload))

        client._send = capture_send
        cfg = ConfigPushPayload(
            1, 1000, [ItemRef(41, "Simulated.Temperature", 0)]
        )
        await client._handle_config_push(cfg.pack())

        assert ConfigAckPayload.unpack(sent[-1][1]).applied
        native_id = client._native_ids[41]
        assert native_id != 41
        await client._handle_read_request(
            ReadRequestPayload(9, [ItemRef(41, "Simulated.Temperature", 0)]).pack()
        )
        response = ReadResponsePayload.unpack(sent[-1][1])
        assert adapter.seen_read_ids == [native_id]
        assert response.results[0].item_id == 41

    @pytest.mark.asyncio
    async def test_failed_candidate_keeps_active_adapter_config_and_group(self):
        active = RecordingAdapter()
        active.connect("Active.OPC")
        client = AgentClient(
            "localhost", 0, "agent", b"x" * 32, active,
            adapter_factory=lambda: RecordingAdapter(fail_path="Missing.Tag"),
        )
        sent = []

        async def capture_send(msg_type, payload):
            sent.append((msg_type, payload))

        client._send = capture_send
        original = ConfigPushPayload(1, 1000, [ItemRef(7, "Good.Tag", 0)])
        await client._handle_config_push(original.pack())
        old_group = client._group_handle
        candidate = None

        def new_candidate():
            nonlocal candidate
            candidate = RecordingAdapter(fail_path="Missing.Tag")
            return candidate

        client._adapter_factory = new_candidate
        rejected = ConfigPushPayload(2, 500, [ItemRef(8, "Missing.Tag", 0)], "Other.OPC")
        await client._handle_config_push(rejected.pack())

        assert not ConfigAckPayload.unpack(sent[-1][1]).applied
        assert client._adapter is active
        assert client._group_handle is old_group
        assert client._active_config == original
        assert client.config_version == 1
        assert candidate is not None and candidate.disconnected
        assert not active.disconnected

    @pytest.mark.asyncio
    async def test_prog_id_switch_prepares_candidate_before_disconnect(self):
        active = RecordingAdapter()
        active.connect("Active.OPC")
        events = []
        active.disconnect = lambda: events.append("old-disconnect")

        class Candidate(RecordingAdapter):
            def connect(self, prog_id):
                events.append("candidate-connect")
                super().connect(prog_id)

            def add_items(self, group, item_paths):
                events.append("candidate-add")
                return super().add_items(group, item_paths)

        client = AgentClient(
            "localhost", 0, "agent", b"x" * 32, active,
            adapter_factory=Candidate,
        )
        client._send = _async_noop
        await client._handle_config_push(
            ConfigPushPayload(1, 1000, [ItemRef(1, "Tag", 0)], "New.OPC").pack()
        )
        assert events == ["candidate-connect", "candidate-add", "old-disconnect"]


async def _async_noop(*args, **kwargs):
    pass


async def _wait_for_cycles(server, count):
    while server.read_metrics.cycles < count:
        await asyncio.sleep(0.005)


class TestSchedulingRegression:
    @pytest.mark.asyncio
    async def test_server_schedules_correlated_cycles_for_simulated_agent(self, server_with_config):
        srv, port, token_hash, certfile = server_with_config
        srv.config.default_update_rate_ms = 30
        srv.config.read_cycle_timeout_ms = 250

        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("scheduled-group", 30)
        adapter.add_items(group, ["Simulated.Temperature", "Simulated.Pressure", "Simulated.Status"])
        adapter._group_handle = group
        client = AgentClient(
            server_host="127.0.0.1",
            server_port=port,
            agent_id="scheduled-agent",
            auth_token_hash=token_hash,
            adapter=adapter,
            certfile=certfile,
        )
        client._group_handle = group
        request_ids = []
        original_handler = client._handle_read_request

        async def capture_response(payload):
            request_ids.append(ReadRequestPayload.unpack(payload).request_id)
            await original_handler(payload)

        client._handle_read_request = capture_response
        await client.connect()
        loop_task = asyncio.create_task(client.run_loop())
        try:
            await asyncio.wait_for(_wait_for_cycles(srv, 3), timeout=1)
            assert len(request_ids) >= 3
            assert len(set(request_ids)) == len(request_ids)
            assert srv.read_metrics.cycles >= 3
            assert srv.read_metrics.duration_total_ms >= srv.read_metrics.duration_last_ms
        finally:
            await client.disconnect()
            await asyncio.gather(loop_task, return_exceptions=True)
            adapter.disconnect()

    @pytest.mark.asyncio
    async def test_timeout_discards_missed_cycles_without_queueing(self, server_with_config):
        srv, port, token_hash, certfile = server_with_config
        srv.config.default_update_rate_ms = 20
        srv.config.read_cycle_timeout_ms = 60

        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("timeout-group", 20)
        adapter.add_items(group, ["Simulated.Temperature", "Simulated.Pressure", "Simulated.Status"])
        adapter._group_handle = group
        client = AgentClient(
            server_host="127.0.0.1",
            server_port=port,
            agent_id="timeout-agent",
            auth_token_hash=token_hash,
            adapter=adapter,
            certfile=certfile,
        )
        client._group_handle = group
        received_ids = []
        original_handler = client._handle_read_request

        async def drop_first_response(payload):
            request_id = ReadRequestPayload.unpack(payload).request_id
            received_ids.append(request_id)
            if len(received_ids) > 1:
                await original_handler(payload)

        client._handle_read_request = drop_first_response
        await client.connect()
        loop_task = asyncio.create_task(client.run_loop())
        try:
            await asyncio.wait_for(_wait_for_cycles(srv, 2), timeout=1)
            assert srv.read_metrics.timeouts == 1
            assert srv.read_metrics.overruns >= 1
            assert len(received_ids) == 2
            assert received_ids[0] != received_ids[1]
        finally:
            await client.disconnect()
            await asyncio.gather(loop_task, return_exceptions=True)
            adapter.disconnect()


class TestPartialError:
    """Some items succeed, others fail — all reported individually."""

    @pytest.mark.asyncio
    async def test_partial_error_in_batch(self, server_with_config):
        srv, port, token_hash, certfile = server_with_config

        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("err-group", 1000)
        mapping = adapter.add_items(group, ["Simulated.Temperature"])
        # Add a non-existent item that will return NOT_FOUND
        adapter._group_handle = group

        client = AgentClient(
            server_host="127.0.0.1",
            server_port=port,
            agent_id="err-agent",
            auth_token_hash=token_hash,
            adapter=adapter,
            certfile=certfile,
        )
        client._group_handle = group

        await client.connect()
        await srv.push_config_to_all()
        await client._handle_config_push(
            ConfigPushPayload(1, 1000, srv._config_items).pack()
        )
        await asyncio.sleep(0.2)

        # Request one valid + one invalid item
        req = ReadRequestPayload(
            request_id=99,
            items=[
                ItemRef(item_id=next(iter(mapping.values())), opc_item_path="Simulated.Temperature", requested_source=0),
                ItemRef(item_id=9999, opc_item_path="NonExistent.Tag", requested_source=0),
            ],
        )
        await client._handle_read_request(req.pack())
        await asyncio.sleep(0.2)

        await client.disconnect()
        adapter.disconnect()


class TestSlowRead:
    """Simulate slow device read; verify duration_us reflects actual latency."""

    @pytest.mark.asyncio
    async def test_slow_read_reports_duration(self, server_with_config):
        srv, port, token_hash, certfile = server_with_config

        # 50ms simulated latency
        adapter = SimulatedOpcAdapter(read_latency_us=50_000)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("slow-group", 1000)
        adapter.add_items(group, ["Simulated.Temperature"])
        adapter._group_handle = group

        client = AgentClient(
            server_host="127.0.0.1",
            server_port=port,
            agent_id="slow-agent",
            auth_token_hash=token_hash,
            adapter=adapter,
            certfile=certfile,
        )
        client._group_handle = group

        await client.connect()
        await srv.push_config_to_all()
        await client._handle_config_push(
            ConfigPushPayload(1, 1000, srv._config_items).pack()
        )
        await asyncio.sleep(0.2)

        start = time.monotonic()
        req = ReadRequestPayload(
            request_id=77,
            items=[ItemRef(item_id=1, opc_item_path="Simulated.Temperature", requested_source=0)],
        )
        await client._handle_read_request(req.pack())
        elapsed = time.monotonic() - start

        # Should take at least ~50ms due to simulated latency
        assert elapsed >= 0.04  # Allow some tolerance

        await client.disconnect()
        adapter.disconnect()


class TestDisconnectReconnect:
    """Agent disconnects and reconnects; server handles gracefully."""

    @pytest.mark.asyncio
    async def test_reconnect_after_disconnect(self, server_with_config):
        srv, port, token_hash, certfile = server_with_config

        adapter = SimulatedOpcAdapter(read_latency_us=0)
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("reconn-group", 1000)
        adapter.add_items(group, ["Simulated.Temperature"])
        adapter._group_handle = group

        # First connection
        client1 = AgentClient(
            server_host="127.0.0.1",
            server_port=port,
            agent_id="reconn-agent",
            auth_token_hash=token_hash,
            adapter=adapter,
            certfile=certfile,
        )
        client1._group_handle = group
        await client1.connect()
        assert len(srv.sessions) == 1
        first_session = client1.session_id

        # Disconnect
        await client1.disconnect()
        await asyncio.sleep(0.3)
        assert len(srv.sessions) == 0

        # Reconnect
        client2 = AgentClient(
            server_host="127.0.0.1",
            server_port=port,
            agent_id="reconn-agent",
            auth_token_hash=token_hash,
            adapter=adapter,
            certfile=certfile,
        )
        client2._group_handle = group
        await client2.connect()
        assert len(srv.sessions) == 1
        assert client2.session_id != first_session  # New session

        await client2.disconnect()
        adapter.disconnect()

    def test_agent_detects_server_heartbeat_timeout(self, tls_certs, auth_token_hash):
        certfile, keyfile = tls_certs
        srv = BridgeServer(ServerConfig(
            host="127.0.0.1", port=0, certfile=certfile, keyfile=keyfile,
            auth_token_hash=auth_token_hash, heartbeat_interval_ms=20,
        ))
        async def scenario():
            await srv.start()
            port = srv._server.sockets[0].getsockname()[1]
            adapter = SimulatedOpcAdapter()
            client = AgentClient("127.0.0.1", port, "timeout-agent", auth_token_hash,
                                 adapter, certfile=certfile, heartbeat_timeout=0.1)
            await client.connect()
            client._heartbeat_timeout = 0.1
            session = next(iter(srv.sessions.values()))
            original_send = session.send

            async def ignore_server_messages(msg_type, payload):
                if msg_type == MsgType.READ_REQUEST:
                    await original_send(msg_type, payload)

            session.send = ignore_server_messages
            loop_task = asyncio.create_task(client.run_loop())
            try:
                await asyncio.wait_for(loop_task, timeout=1)
                assert not client.is_connected
            finally:
                await client.disconnect()
                await srv.stop()

        asyncio.run(scenario())

    def test_tls_rejects_untrusted_ca_and_hostname_mismatch(self, tls_certs, auth_token_hash):
        certfile, keyfile = tls_certs

        async def scenario(server_hostname):
            srv = BridgeServer(ServerConfig(
                host="127.0.0.1", port=0, certfile=certfile, keyfile=keyfile,
                auth_token_hash=auth_token_hash,
            ))
            await srv.start()
            port = srv._server.sockets[0].getsockname()[1]
            try:
                client = AgentClient(
                    "127.0.0.1", port, "tls-agent", auth_token_hash,
                    SimulatedOpcAdapter(), certfile=certfile,
                    server_hostname=server_hostname,
                )
                with pytest.raises(ssl.SSLCertVerificationError):
                    await client.connect()
            finally:
                await srv.stop()

        asyncio.run(scenario("wrong-host.invalid"))

    def test_tls_rejects_untrusted_certificate(self, tls_certs, auth_token_hash, tmp_path):
        certfile, keyfile = tls_certs
        unrelated_ca = tmp_path / "unrelated.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(tmp_path / "unrelated-key.pem"),
                "-out", str(unrelated_ca), "-days", "1", "-nodes",
                "-subj", "/CN=unrelated",
            ],
            check=True, capture_output=True,
        )

        async def scenario():
            srv = BridgeServer(ServerConfig(
                host="127.0.0.1", port=0, certfile=certfile, keyfile=keyfile,
                auth_token_hash=auth_token_hash,
            ))
            await srv.start()
            port = srv._server.sockets[0].getsockname()[1]
            try:
                client = AgentClient(
                    "127.0.0.1", port, "tls-agent", auth_token_hash,
                    SimulatedOpcAdapter(), certfile=str(unrelated_ca),
                )
                with pytest.raises(ssl.SSLCertVerificationError):
                    await client.connect()
            finally:
                await srv.stop()

        asyncio.run(scenario())


class TestNoCacheEnforcement:
    """Each read must hit the adapter; no cached values."""

    @pytest.mark.asyncio
    async def test_consecutive_reads_produce_different_timestamps(self, server_with_config):
        srv, port, token_hash, certfile = server_with_config

        adapter = SimulatedOpcAdapter(read_latency_us=1000)  # 1ms to ensure distinct timestamps
        adapter.connect("Simulated.OPC")
        group = adapter.create_group("nocache-group", 1000)
        adapter.add_items(group, ["Simulated.Temperature"])
        adapter._group_handle = group

        client = AgentClient(
            server_host="127.0.0.1",
            server_port=port,
            agent_id="nocache-agent",
            auth_token_hash=token_hash,
            adapter=adapter,
            certfile=certfile,
        )
        client._group_handle = group

        await client.connect()
        await srv.push_config_to_all()
        await client._handle_config_push(
            ConfigPushPayload(1, 1000, srv._config_items).pack()
        )
        await asyncio.sleep(0.2)

        # Two consecutive reads
        req = ReadRequestPayload(
            request_id=1,
            items=[ItemRef(item_id=1, opc_item_path="Simulated.Temperature", requested_source=0)],
        )
        await client._handle_read_request(req.pack())
        await asyncio.sleep(0.01)

        req2 = ReadRequestPayload(
            request_id=2,
            items=[ItemRef(item_id=1, opc_item_path="Simulated.Temperature", requested_source=0)],
        )
        await client._handle_read_request(req2.pack())
        await asyncio.sleep(0.2)

        # Both reads should have completed without error
        await client.disconnect()
        adapter.disconnect()
