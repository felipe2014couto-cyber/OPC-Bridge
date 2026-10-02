"""OPC-Bridge agent client: TLS connection, handshake and message loop.

Connects to the central server, authenticates, receives configuration,
executes reads via an OPC adapter (simulated or real), and sends responses.
Follows the protocol contract in docs/contracts/protocol.md.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import struct
import time
from dataclasses import replace
from typing import Callable

from opc_bridge.adapters.base import OpcAdapter
from opc_bridge.agent.inspection import inspect_opc
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
    ItemStatus,
    MsgType,
    ReadRequestPayload,
    ReadResponsePayload,
    ValueType,
    frame_message,
    unframe_message,
)
from opc_bridge.protocol.inspection import CAPABILITY, InspectionRequest, InspectionResponse

logger = logging.getLogger(__name__)

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
        adapter_factory: Callable[[], OpcAdapter] | None = None,
        server_hostname: str | None = None,
    ) -> None:
        self._host = server_host
        self._port = server_port
        self._agent_id = agent_id
        self._auth_token_hash = auth_token_hash
        self._adapter = adapter
        self._certfile = certfile  # Trusted CA bundle (legacy option name).
        self._server_hostname = server_hostname or server_host
        self._adapter_factory = adapter_factory
        self._active_config: ConfigPushPayload | None = None
        self._native_ids: dict[int, int] = {}
        self._config_attempt = 0
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
        self._inspection_task: asyncio.Task | None = None

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
        ssl_ctx = ssl.create_default_context(cafile=self._certfile or None)
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port, ssl=ssl_ctx, server_hostname=self._server_hostname
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
            capabilities=[CAPABILITY] if self._adapter_factory is not None else [],
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
            elif header.msg_type == MsgType.OPC_INSPECT_REQUEST:
                await self._handle_inspection_request(payload)
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
        if self._inspection_task is not None:
            self._inspection_task.cancel()
            await asyncio.gather(self._inspection_task, return_exceptions=True)
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

    async def _handle_inspection_request(self, payload: bytes) -> None:
        request = InspectionRequest.unpack(payload)
        if self._inspection_task is not None and not self._inspection_task.done():
            await self._send(MsgType.OPC_INSPECT_RESPONSE,
                             InspectionResponse(request.request_id, error="busy").pack())
            return

        async def execute() -> None:
            try:
                response = await asyncio.get_running_loop().run_in_executor(
                    None, inspect_opc, request, self._adapter_factory, self._adapter
                )
                await self._send(MsgType.OPC_INSPECT_RESPONSE, response.pack())
            except Exception:  # noqa: BLE001 - transport cleanup; never log provider details.
                logger.warning("Inspection response could not be delivered")

        self._inspection_task = asyncio.create_task(execute())

    async def _handle_config_push(self, payload: bytes) -> None:
        """Process CONFIG_PUSH and send CONFIG_ACK."""
        # Even a malformed body can be rejected with its version when available.
        if len(payload) < 4:
            raise ValueError("CONFIG_PUSH has no configuration version")
        version = struct.unpack_from("<I", payload)[0]
        candidate = self._adapter
        staged_group = None
        applied = False
        try:
            cfg = ConfigPushPayload.unpack(payload)
            if cfg.config_version < self._config_version:
                raise ValueError("Stale configuration version")
            if self._active_config and cfg.config_version == self._config_version:
                if cfg != self._active_config:
                    raise ValueError("Configuration changed without a new version")
                applied = True
            else:
                if cfg.update_rate_ms == 0:
                    raise ValueError("Update rate must be positive")
                ids = [item.item_id for item in cfg.items]
                paths = [item.opc_item_path for item in cfg.items]
                if len(set(ids)) != len(ids):
                    raise ValueError("Duplicate central item IDs")
                if any(not path.strip() for path in paths):
                    raise ValueError("Empty OPC item path")
                if any(item.requested_source != 0 for item in cfg.items):
                    raise ValueError("Only Device reads are supported")
                if cfg.opc_prog_id and candidate is self._adapter:
                    if self._adapter_factory is None:
                        raise ValueError("ProgID configuration requires an isolated adapter factory")
                    candidate = self._adapter_factory()
                    if candidate is self._adapter:
                        raise ValueError("Adapter factory must return a separate instance")
                if cfg.opc_prog_id:
                    candidate.connect(cfg.opc_prog_id)
                self._config_attempt += 1
                staged_group = candidate.create_group(
                    f"config_{id(self)}_{self._config_attempt}", cfg.update_rate_ms
                )
                mapping = candidate.add_items(staged_group, list(dict.fromkeys(paths)))
                if any(path not in mapping for path in paths):
                    raise ValueError("Adapter did not register every configured item")
                if any(not isinstance(iid, int) or iid <= 0 for iid in mapping.values()):
                    raise ValueError("Invalid adapter item IDs")
                if len(set(mapping.values())) != len(mapping):
                    raise ValueError("Ambiguous adapter item IDs")
                native_ids = {item.item_id: mapping[item.opc_item_path] for item in cfg.items}
                old_adapter, old_group = self._adapter, self._group_handle
                # No await between preparing and publishing this complete state.
                self._adapter = candidate
                self._group_handle = staged_group
                self._item_mapping = dict(zip(ids, paths))
                self._native_ids = native_ids
                self._active_config = cfg
                self._config_version = cfg.config_version
                applied = True
                try:
                    if old_adapter is not candidate:
                        old_adapter.disconnect()
                    elif old_group is not None:
                        old_adapter.remove_group(old_group)
                except (RuntimeError, OSError, ConnectionError, ValueError):
                    logger.exception("Failed to release superseded configuration resources")
                logger.info("Config v%d applied: %d items", version, len(cfg.items))
        except Exception as exc:  # Adapter providers may raise vendor-specific COM exceptions.  # noqa: BLE001 - provider-specific exception boundary.
            logger.warning("Config v%d rejected: %s", version, exc)
            try:
                if candidate is not self._adapter and candidate is not None:
                    candidate.disconnect()
                elif staged_group is not None:
                    candidate.remove_group(staged_group)
            except (RuntimeError, OSError, ConnectionError, ValueError):
                logger.exception("Failed to release rejected configuration resources")

        ack = ConfigAckPayload(config_version=version, applied=applied)
        await self._send(MsgType.CONFIG_ACK, ack.pack())

    async def _handle_read_request(self, payload: bytes) -> None:
        """Translate central IDs to adapter IDs for a fresh Device read."""
        req = ReadRequestPayload.unpack(payload)
        start_us = int(time.time() * 1_000_000)
        native_ids = list(dict.fromkeys(
            self._native_ids[item.item_id] for item in req.items
            if item.item_id in self._native_ids and item.requested_source == 0
        ))
        raw_results = []
        if self._group_handle is not None and native_ids:
            try:
                raw_results = self._adapter.read_device(self._group_handle, native_ids)
            except Exception:
                logger.exception("Device read failed")
        res_by_id = {r.item_id: r for r in raw_results}
        results = []
        for item in req.items:
            native_id = self._native_ids.get(item.item_id)
            result = res_by_id.get(native_id) if item.requested_source == 0 else None
            if result is not None:
                results.append(replace(result, item_id=item.item_id))
            else:
                results.append(ItemResult(
                    item_id=item.item_id,
                    status=ItemStatus.NOT_FOUND if native_id is None else ItemStatus.ERROR,
                    value_type=ValueType.BLOB, quality=0, timestamp_us=start_us,
                    value=b"", error_code=0x80040001,
                ))
        resp = ReadResponsePayload(
            request_id=req.request_id,
            duration_us=int(time.time() * 1_000_000) - start_us,
            results=results,
        )
        await self._send(MsgType.READ_RESPONSE, resp.pack())

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
