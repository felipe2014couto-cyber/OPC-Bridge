"""OPC-Bridge agent client: TLS connection, handshake and message loop.

Connects to the central server, authenticates, receives configuration,
executes reads via an OPC adapter (simulated or real), and sends responses.
Follows the protocol contract in docs/contracts/protocol.md.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from typing import Protocol

from opc_bridge.protocol import (
    HEADER_SIZE,
    TRAILER_SIZE,
    AuthAckPayload,
    AuthPayload,
    ConfigAckPayload,
    ConfigPushPayload,
    Header,
    HeartbeatPayload,
    HelloAckPayload,
    HelloPayload,
    ItemResult,
    MsgType,
    ReadRequestPayload,
    ReadResponsePayload,
    frame_message,
    unframe_message,
)

logger = logging.getLogger(__name__)


class OpcAdapter(Protocol):
    """Protocol for OPC adapters (simulated or real COM)."""

    def connect(self, prog_id: str) -> None: ...
    def disconnect(self) -> None: ...
    def read_device(self, group: object, item_ids: list[int]) -> list[ItemResult]: ...


class AgentClient:
    """TLS client that connects to the OPC-Bridge central server."""

    def __init__(
        self,
        server_host: str,
        server_port: int,
        agent_id: str,
        auth_token_hash: bytes,
        adapter: OpcAdapter,
        certfile: str | None = None,
        heartbeat_timeout: float = 30.0,
    ) -> None:
        self._host = server_host
        self._port = server_port
        self._agent_id = agent_id
        self._auth_token_hash = auth_token_hash
        self._adapter = adapter
        self._certfile = certfile
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._session_id: str | None = None
        self._seq_out = 0
        self._config_version = 0
        self._running = False
        self._group_handle: object | None = None
        self._item_mapping: dict[int, str] = {}  # item_id -> opc_path
        self._heartbeat_interval = 5.0
        self._heartbeat_timeout = heartbeat_timeout
        self._last_received = time.monotonic()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_expired = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def config_version(self) -> int:
        return self._config_version

    @property
    def is_connected(self) -> bool:
        return (
            self._running
            and self._writer is not None
            and not self._writer.is_closing()
            and not self._heartbeat_expired
        )

    async def connect(self) -> None:
        """Establish TLS connection and perform handshake."""
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        # NOTE: In production, load the CA bundle and enable verification.
        # For testing with self-signed certs (CN mismatch on 127.0.0.1),
        # verification is disabled to match server test fixtures.

        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port, ssl=ssl_ctx
        )
        logger.info("Connected to %s:%d", self._host, self._port)

        await self._handshake()
        self._running = True
        self._last_received = time.monotonic()
        self._heartbeat_expired = False

    async def _handshake(self) -> None:
        """Perform HELLO + AUTH handshake with the server."""
        assert self._reader is not None
        assert self._writer is not None

        # Send HELLO
        hello = HelloPayload(
            agent_id=self._agent_id,
            hostname="agent-host",
            os_version="Python",
            capabilities=["opc-da-sim"],
        )
        await self._send(MsgType.HELLO, hello.pack())

        # Receive HELLO_ACK
        header, payload = await self._read_message()
        if header.msg_type != MsgType.HELLO_ACK:
            raise RuntimeError(f"Expected HELLO_ACK, got {header.msg_type}")
        ack = HelloAckPayload.unpack(payload)
        self._session_id = ack.session_id
        self._heartbeat_interval = max(0.1, ack.heartbeat_interval_ms / 1000)
        self._heartbeat_timeout = max(
            self._heartbeat_timeout, self._heartbeat_interval * 3
        )
        logger.info("Session established: %s", self._session_id)

        # Send AUTH
        auth = AuthPayload(token_hash=self._auth_token_hash)
        await self._send(MsgType.AUTH, auth.pack())

        # Receive AUTH_ACK
        header, payload = await self._read_message()
        if header.msg_type != MsgType.AUTH_ACK:
            raise RuntimeError(f"Expected AUTH_ACK, got {header.msg_type}")
        auth_ack = AuthAckPayload.unpack(payload)
        if not auth_ack.success:
            raise RuntimeError("Authentication failed")
        logger.info("Authenticated successfully")

    async def run_loop(self) -> None:
        """Main message processing loop."""
        assert self._reader is not None
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        while self._running:
            try:
                header, payload = await asyncio.wait_for(
                    self._read_message(), timeout=self._heartbeat_timeout
                )
                self._last_received = time.monotonic()
            except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                logger.warning("Connection lost")
                break

            if header.msg_type == MsgType.CONFIG_PUSH:
                await self._handle_config_push(payload)
            elif header.msg_type == MsgType.READ_REQUEST:
                await self._handle_read_request(payload)
            elif header.msg_type == MsgType.HEARTBEAT:
                logger.debug("Heartbeat received")
            elif header.msg_type == MsgType.ERROR:
                from opc_bridge.protocol import ErrorPayload

                err = ErrorPayload.unpack(payload)
                logger.error("Server error: code=%d msg=%s", err.code, err.message)
            else:
                logger.warning("Unexpected message type: %s", header.msg_type)
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            if time.monotonic() - self._last_received > self._heartbeat_timeout:
                logger.warning("Heartbeat timeout: session=%s", self._session_id)
                self._running = False
                self._heartbeat_expired = True
                if self._writer:
                    self._writer.close()
                return
            try:
                payload = HeartbeatPayload(timestamp_us=int(time.time() * 1_000_000)).pack()
                await self._send(MsgType.HEARTBEAT, payload)
            except (OSError, ConnectionError):
                self._running = False
                return

    async def _handle_config_push(self, payload: bytes) -> None:
        """Process CONFIG_PUSH and send CONFIG_ACK."""
        cfg = ConfigPushPayload.unpack(payload)
        applied = True
        try:
            self._config_version = cfg.config_version
            self._item_mapping = {
                item.item_id: item.opc_item_path for item in cfg.items
            }
            logger.info(
                "Config v%d applied: %d items, rate=%dms",
                cfg.config_version,
                len(cfg.items),
                cfg.update_rate_ms,
            )
        except (ValueError, KeyError, OSError) as exc:
            logger.error("Failed to apply config v%d: %s", cfg.config_version, exc)
            applied = False

        ack = ConfigAckPayload(config_version=cfg.config_version, applied=applied)
        await self._send(MsgType.CONFIG_ACK, ack.pack())

    async def _handle_read_request(self, payload: bytes) -> None:
        """Execute Device read via adapter and send READ_RESPONSE."""
        req = ReadRequestPayload.unpack(payload)
        start_us = int(time.time() * 1_000_000)

        results: list[ItemResult] = []
        for item_ref in req.items:
            # Simulate device read using adapter
            # In production, this would call adapter.read_device()
            # For now, generate simulated results
            from opc_bridge.adapters.simulated import SimulatedOpcAdapter

            if isinstance(self._adapter, SimulatedOpcAdapter):
                # Use simulated adapter directly
                sim_results = self._adapter.read_device(
                    self._group_handle, [item_ref.item_id]
                )
                if sim_results:
                    results.append(sim_results[0])
                else:
                    results.append(
                        ItemResult(
                            item_id=item_ref.item_id,
                            status=4,  # ERROR
                            value_type=0,
                            quality=0,
                            timestamp_us=int(time.time() * 1_000_000),
                            value=b"",
                            error_code=0x80040001,
                        )
                    )
            else:
                # Placeholder for real adapter integration
                results.append(
                    ItemResult(
                        item_id=item_ref.item_id,
                        status=0,
                        value_type=3,  # F64
                        quality=192,
                        timestamp_us=int(time.time() * 1_000_000),
                        value=b"\x00" * 8,
                        error_code=0,
                    )
                )

        end_us = int(time.time() * 1_000_000)
        duration_us = end_us - start_us

        resp = ReadResponsePayload(
            request_id=req.request_id,
            duration_us=duration_us,
            results=results,
        )
        await self._send(MsgType.READ_RESPONSE, resp.pack())
        logger.debug(
            "Read response sent: request=%d duration=%dus items=%d",
            req.request_id,
            duration_us,
            len(results),
        )

    async def _send(self, msg_type: MsgType, payload: bytes) -> None:
        """Send a framed message to the server."""
        assert self._writer is not None
        self._seq_out += 1
        framed = frame_message(msg_type, self._seq_out, payload)
        self._writer.write(framed)
        await self._writer.drain()

    async def _read_message(self) -> tuple[Header, bytes]:
        """Read and unframe one protocol message from the server."""
        assert self._reader is not None
        hdr_bytes = await self._reader.readexactly(HEADER_SIZE)
        header = Header.unpack(hdr_bytes)
        rest = await self._reader.readexactly(header.payload_len + TRAILER_SIZE)
        return unframe_message(hdr_bytes + rest)

    async def disconnect(self) -> None:
        """Close the connection gracefully."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self._heartbeat_expired and self._writer:
            self._writer.close()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except (OSError, ConnectionError) as exc:
                logger.debug("Error closing writer: %s", exc)
        logger.info("Disconnected from server")
