"""Integration tests for OPC-Bridge central server (T3)."""
from __future__ import annotations

import asyncio
import ssl
import struct
import tempfile
import time
from pathlib import Path

import pytest

from opc_bridge.protocol import (
    MsgType,
    AuthPayload,
    ConfigAckPayload,
    ConfigPushPayload,
    HelloAckPayload,
    HelloPayload,
    ItemRef,
    ReadResponsePayload,
    ItemResult,
    ItemStatus,
    ValueType,
    frame_message,
    unframe_message,
)
from opc_bridge.server import BridgeServer, ServerConfig


@pytest.fixture
def tls_certs(tmp_path: Path):
    """Generate self-signed TLS certificates for testing."""
    certfile = tmp_path / "cert.pem"
    keyfile = tmp_path / "key.pem"
    # Use openssl to generate self-signed cert
    import subprocess
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(keyfile), "-out", str(certfile),
        "-days", "1", "-nodes", "-subj", "/CN=localhost",
    ], check=True, capture_output=True)
    return str(certfile), str(keyfile)


@pytest.fixture
def auth_token_hash():
    return b"\xaa\xbb\xcc\xdd" * 8  # 32 bytes


@pytest.fixture
async def server(tls_certs, auth_token_hash):
    """Start a BridgeServer with TLS and return it."""
    certfile, keyfile = tls_certs
    config = ServerConfig(
        host="127.0.0.1",
        port=0,  # Let OS pick a free port
        certfile=certfile,
        keyfile=keyfile,
        auth_token_hash=auth_token_hash,
        heartbeat_interval_ms=5000,
        default_update_rate_ms=1000,
    )
    srv = BridgeServer(config)
    await srv.start()
    # Get the actual port
    port = srv._server.sockets[0].getsockname()[1]
    yield srv, port, auth_token_hash
    await srv.stop()


async def _connect_tls(port: int, certfile: str):
    """Create a TLS connection to the test server."""
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ssl_ctx)
    return reader, writer


async def _read_framed(reader):
    """Read one framed message from the stream."""
    from opc_bridge.protocol.messages import HEADER_SIZE, TRAILER_SIZE, Header
    hdr_bytes = await reader.readexactly(HEADER_SIZE)
    header = Header.unpack(hdr_bytes)
    rest = await reader.readexactly(header.payload_len + TRAILER_SIZE)
    return unframe_message(hdr_bytes + rest)


class TestServerHandshake:
    @pytest.mark.asyncio
    async def test_hello_auth_success(self, server):
        srv, port, token_hash = server
        certfile = srv.config.certfile
        reader, writer = await _connect_tls(port, certfile)

        try:
            # Send HELLO
            hello = HelloPayload(
                agent_id="test-agent-001",
                hostname="test-host",
                os_version="Linux Test",
                capabilities=["opc-da"],
            )
            writer.write(frame_message(MsgType.HELLO, 1, hello.pack()))
            await writer.drain()

            # Receive HELLO_ACK
            header, payload = await _read_framed(reader)
            assert header.msg_type == MsgType.HELLO_ACK
            ack = HelloAckPayload.unpack(payload)
            assert ack.session_id.startswith("sess-")
            assert ack.heartbeat_interval_ms == 5000

            # Send AUTH
            auth = AuthPayload(token_hash=token_hash)
            writer.write(frame_message(MsgType.AUTH, 2, auth.pack()))
            await writer.drain()

            # Receive AUTH_ACK
            header, payload = await _read_framed(reader)
            assert header.msg_type == MsgType.AUTH_ACK
            from opc_bridge.protocol.messages import AuthAckPayload
            auth_ack = AuthAckPayload.unpack(payload)
            assert auth_ack.success is True

            # Verify session registered
            assert len(srv.sessions) == 1
            session = list(srv.sessions.values())[0]
            assert session.agent_id == "test-agent-001"
            assert session.hostname == "test-host"
        finally:
            writer.close()
            await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_auth_failure(self, server):
        srv, port, _ = server
        certfile = srv.config.certfile
        reader, writer = await _connect_tls(port, certfile)

        try:
            # Send HELLO
            hello = HelloPayload(
                agent_id="bad-agent",
                hostname="bad-host",
                os_version="Linux",
                capabilities=[],
            )
            writer.write(frame_message(MsgType.HELLO, 1, hello.pack()))
            await writer.drain()

            # Receive HELLO_ACK
            await _read_framed(reader)

            # Send AUTH with wrong token
            auth = AuthPayload(token_hash=b"\x00" * 32)
            writer.write(frame_message(MsgType.AUTH, 2, auth.pack()))
            await writer.drain()

            # Receive AUTH_ACK with failure
            header, payload = await _read_framed(reader)
            assert header.msg_type == MsgType.AUTH_ACK
            from opc_bridge.protocol.messages import AuthAckPayload
            auth_ack = AuthAckPayload.unpack(payload)
            assert auth_ack.success is False

            # Session should NOT be registered
            assert len(srv.sessions) == 0
        finally:
            writer.close()
            await writer.wait_closed()


class TestConfigPush:
    @pytest.mark.asyncio
    async def test_push_config_to_agent(self, server):
        srv, port, token_hash = server
        certfile = srv.config.certfile

        # Set config on server
        items = [
            ItemRef(item_id=1, opc_item_path="Tag.A", requested_source=0),
            ItemRef(item_id=2, opc_item_path="Tag.B", requested_source=0),
        ]
        srv.set_config(items, version=5)

        reader, writer = await _connect_tls(port, certfile)
        try:
            # Handshake
            hello = HelloPayload(agent_id="cfg-agent", hostname="h", os_version="o", capabilities=[])
            writer.write(frame_message(MsgType.HELLO, 1, hello.pack()))
            await writer.drain()
            await _read_framed(reader)  # HELLO_ACK

            auth = AuthPayload(token_hash=token_hash)
            writer.write(frame_message(MsgType.AUTH, 2, auth.pack()))
            await writer.drain()
            await _read_framed(reader)  # AUTH_ACK

            # Push config
            await srv.push_config_to_all()

            # Agent receives CONFIG_PUSH
            header, payload = await _read_framed(reader)
            assert header.msg_type == MsgType.CONFIG_PUSH
            cfg = ConfigPushPayload.unpack(payload)
            assert cfg.config_version == 5
            assert cfg.update_rate_ms == 1000
            assert len(cfg.items) == 2

            # Agent sends CONFIG_ACK
            ack = ConfigAckPayload(config_version=5, applied=True)
            writer.write(frame_message(MsgType.CONFIG_ACK, 3, ack.pack()))
            await writer.drain()

            # Give server time to process
            await asyncio.sleep(0.1)

            # Verify session config version updated
            session = list(srv.sessions.values())[0]
            assert session.config_version == 5
        finally:
            writer.close()
            await writer.wait_closed()


class TestMessageLoop:
    @pytest.mark.asyncio
    async def test_heartbeat_processing(self, server):
        srv, port, token_hash = server
        certfile = srv.config.certfile
        reader, writer = await _connect_tls(port, certfile)

        try:
            # Handshake
            hello = HelloPayload(agent_id="hb-agent", hostname="h", os_version="o", capabilities=[])
            writer.write(frame_message(MsgType.HELLO, 1, hello.pack()))
            await writer.drain()
            await _read_framed(reader)

            auth = AuthPayload(token_hash=token_hash)
            writer.write(frame_message(MsgType.AUTH, 2, auth.pack()))
            await writer.drain()
            await _read_framed(reader)

            # Send HEARTBEAT
            from opc_bridge.protocol.messages import HeartbeatPayload
            hb = HeartbeatPayload(timestamp_us=int(time.time() * 1_000_000))
            writer.write(frame_message(MsgType.HEARTBEAT, 3, hb.pack()))
            await writer.drain()

            await asyncio.sleep(0.1)

            # Verify last_heartbeat updated
            session = list(srv.sessions.values())[0]
            assert session.last_heartbeat > 0
        finally:
            writer.close()
            await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_read_response_processing(self, server):
        srv, port, token_hash = server
        certfile = srv.config.certfile
        reader, writer = await _connect_tls(port, certfile)

        try:
            # Handshake
            hello = HelloPayload(agent_id="rr-agent", hostname="h", os_version="o", capabilities=[])
            writer.write(frame_message(MsgType.HELLO, 1, hello.pack()))
            await writer.drain()
            await _read_framed(reader)

            auth = AuthPayload(token_hash=token_hash)
            writer.write(frame_message(MsgType.AUTH, 2, auth.pack()))
            await writer.drain()
            await _read_framed(reader)

            # Send READ_RESPONSE
            results = [
                ItemResult(
                    item_id=1, status=ItemStatus.OK, value_type=ValueType.F64,
                    quality=192, timestamp_us=1000,
                    value=struct.pack("<d", 3.14), error_code=0,
                ),
            ]
            resp = ReadResponsePayload(request_id=42, duration_us=500, results=results)
            writer.write(frame_message(MsgType.READ_RESPONSE, 3, resp.pack()))
            await writer.drain()

            await asyncio.sleep(0.1)
            # No exception means the server processed it correctly
        finally:
            writer.close()
            await writer.wait_closed()


class TestServerLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self, tls_certs, auth_token_hash):
        certfile, keyfile = tls_certs
        config = ServerConfig(
            host="127.0.0.1",
            port=0,
            certfile=certfile,
            keyfile=keyfile,
            auth_token_hash=auth_token_hash,
        )
        srv = BridgeServer(config)
        await srv.start()
        assert srv._running is True
        assert srv._server is not None
        await srv.stop()
        assert srv._running is False
        assert len(srv.sessions) == 0

    @pytest.mark.asyncio
    async def test_disconnect_removes_session(self, server):
        srv, port, token_hash = server
        certfile = srv.config.certfile
        reader, writer = await _connect_tls(port, certfile)

        # Handshake
        hello = HelloPayload(agent_id="disc-agent", hostname="h", os_version="o", capabilities=[])
        writer.write(frame_message(MsgType.HELLO, 1, hello.pack()))
        await writer.drain()
        await _read_framed(reader)

        auth = AuthPayload(token_hash=token_hash)
        writer.write(frame_message(MsgType.AUTH, 2, auth.pack()))
        await writer.drain()
        await _read_framed(reader)

        assert len(srv.sessions) == 1

        # Close connection
        writer.close()
        await writer.wait_closed()

        # Give server time to detect disconnect
        await asyncio.sleep(0.2)

        assert len(srv.sessions) == 0