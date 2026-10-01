"""BridgeServer integration tests using simulated asyncio streams."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from opc_bridge.protocol import (
    HEADER_SIZE,
    TRAILER_SIZE,
    AuthAckPayload,
    AuthPayload,
    ConfigAckPayload,
    ConfigPushPayload,
    Header,
    HelloPayload,
    ItemRef,
    MsgType,
    frame_message,
    unframe_message,
)
from opc_bridge.protocol.messages import HeartbeatPayload
from opc_bridge.server import BridgeServer, ServerConfig
from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database


class _FakeWriter:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.reader.feed_eof()

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)

    def get_extra_info(self, name: str):
        return None


async def _read_message(reader: asyncio.StreamReader):
    header_data = await reader.readexactly(HEADER_SIZE)
    header = Header.unpack(header_data)
    rest = await reader.readexactly(header.payload_len + TRAILER_SIZE)
    return unframe_message(header_data + rest)


async def _wait_until(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    assert predicate()


async def _connect(server: BridgeServer, token_hash: bytes):
    reader = asyncio.StreamReader()
    writer = _FakeWriter(reader)
    hello = HelloPayload("agent-a", "host-a", "os-test", ["opc-da"])
    reader.feed_data(
        frame_message(MsgType.HELLO, 1, hello.pack())
        + frame_message(MsgType.AUTH, 2, AuthPayload(token_hash).pack())
    )
    handler = asyncio.create_task(server._handle_client(reader, writer))
    await _wait_until(lambda: bool(server.sessions) or handler.done())
    assert server.sessions
    auth_header, auth_bytes = unframe_message(writer.frames[-1])
    assert auth_header.msg_type == MsgType.AUTH_ACK
    assert AuthAckPayload.unpack(auth_bytes).success
    session = next(iter(server.sessions.values()))
    return reader, writer, session.session_id, handler


def _database(tmp_path: Path):
    database = sqlite_for_tests(str(tmp_path / "server.sqlite"))
    upgrade_database(database)
    token_hash = hashlib.sha256(b"agent-secret").digest()
    with database.session() as repo:
        repo.add_agent("agent-a", "Agent A")
        repo.add_credential("agent-a", "credential-a", token_hash.hex())
    server = BridgeServer(
        ServerConfig(
            host="127.0.0.1",
            port=0,
            persistence=database,
            config_ack_timeout_ms=80,
        )
    )
    server._running = True
    return database, token_hash, server


def _run(coro):
    asyncio.run(coro)


def test_persisted_session_heartbeat_disconnect_and_applied_ack(tmp_path: Path) -> None:
    async def scenario() -> None:
        database, token_hash, server = _database(tmp_path)
        reader, writer, session_id, handler = await _connect(server, token_hash)
        with database.session() as repo:
            session = repo.get_session(session_id)
        assert session is not None
        assert session.state == "connected"
        assert session.hostname == "host-a"

        heartbeat = HeartbeatPayload(timestamp_us=int(time.time() * 1_000_000))
        reader.feed_data(frame_message(MsgType.HEARTBEAT, 3, heartbeat.pack()))
        await _wait_until(
            lambda: _session_has_heartbeat(database, session_id)
        )
        with database.session() as repo:
            session = repo.get_session(session_id)
        assert session is not None
        assert session.last_heartbeat_at is not None
        assert json.loads(session.observed_state_json)["last_message"] == "HEARTBEAT"

        server.set_config([ItemRef(1, "Tag.A")], version=4)
        await server.push_config_to_all()
        header, payload = unframe_message(writer.frames[-1])
        assert header.msg_type == MsgType.CONFIG_PUSH
        assert ConfigPushPayload.unpack(payload).config_version == 4
        reader.feed_data(frame_message(MsgType.CONFIG_ACK, 4, ConfigAckPayload(4, True).pack()))
        await _wait_until(lambda: _operation_has_status(database, "applied"))
        with database.session() as repo:
            operations = repo.list_operations("agent-a")
            session = repo.get_session(session_id)
        assert len(operations) == 1
        assert operations[0].status == "applied"
        assert session is not None and session.applied_config_version == 4

        reader.feed_eof()
        await handler
        with database.session() as repo:
            session = repo.get_session(session_id)
        assert session is not None and session.state == "disconnected"
        await server.stop()

    _run(scenario())


def test_persisted_config_ack_rejection_does_not_mark_applied(tmp_path: Path) -> None:
    async def scenario() -> None:
        database, token_hash, server = _database(tmp_path)
        reader, _writer, session_id, handler = await _connect(server, token_hash)
        server.set_config([ItemRef(1, "Tag.Rejected")], version=5)
        await server.push_config_to_all()
        reader.feed_data(frame_message(MsgType.CONFIG_ACK, 3, ConfigAckPayload(5, False).pack()))
        await _wait_until(lambda: _operation_has_status(database, "rejected"))
        with database.session() as repo:
            operations = repo.list_operations("agent-a")
            session = repo.get_session(session_id)
        assert operations[0].status == "rejected"
        assert session is not None and session.applied_config_version is None
        assert server.sessions[session_id].config_version == 0
        reader.feed_eof()
        await handler
        await server.stop()

    _run(scenario())


def test_config_ack_timeout_is_persisted_as_expired(tmp_path: Path) -> None:
    async def scenario() -> None:
        database, token_hash, server = _database(tmp_path)
        reader, _, _, handler = await _connect(server, token_hash)
        server.set_config([ItemRef(1, "Tag.Timeout")], version=6)
        await server.push_config_to_all()
        await _wait_until(lambda: _operation_has_status(database, "expired"))
        assert server.sessions[next(iter(server.sessions))].config_version == 0
        reader.feed_eof()
        await handler
        await server.stop()

    _run(scenario())


def test_restart_recovers_snapshot_and_applied_session_version(tmp_path: Path) -> None:
    async def scenario() -> None:
        database, token_hash, server = _database(tmp_path)
        reader, _, session_id, handler = await _connect(server, token_hash)
        server.set_config([ItemRef(1, "Tag.Restore")], version=7)
        await server.push_config_to_all()
        reader.feed_data(frame_message(MsgType.CONFIG_ACK, 3, ConfigAckPayload(7, True).pack()))
        await _wait_until(lambda: _operation_has_status(database, "applied"))
        reader.feed_eof()
        await handler
        await server.stop()

        restarted = BridgeServer(ServerConfig(persistence=database))
        assert restarted.config_version == 7
        assert restarted._config_items == [ItemRef(1, "Tag.Restore")]
        with database.session() as repo:
            operations = repo.list_operations("agent-a")
            prior_session = repo.get_session(session_id)
        assert operations[0].status == "applied"
        assert prior_session is not None and prior_session.state == "disconnected"

    _run(scenario())


def _session_has_heartbeat(database, session_id: str) -> bool:
    with database.session() as repo:
        session = repo.get_session(session_id)
    return session is not None and session.last_heartbeat_at is not None


def _operation_has_status(database, status: str) -> bool:
    with database.session() as repo:
        operations = repo.list_operations("agent-a")
    return bool(operations and operations[-1].status == status)
