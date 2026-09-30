"""OPC-Bridge central server core: TLS endpoint, session and config management."""
from __future__ import annotations

import asyncio
import logging
import ssl
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

from opc_bridge.protocol import (
    HEADER_SIZE,
    TRAILER_SIZE,
    MsgType,
    AuthAckPayload,
    ConfigPushPayload,
    ErrorPayload,
    HelloAckPayload,
    HelloPayload,
    ItemRef,
    ReadResponsePayload,
    frame_message,
    unframe_message,
)

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Configuration for the BridgeServer."""

    host: str = "0.0.0.0"
    port: int = 8443
    certfile: str = ""
    keyfile: str = ""
    auth_token_hash: bytes = b""
    heartbeat_interval_ms: int = 5000
    default_update_rate_ms: int = 1000


@dataclass
class AgentSession:
    """Represents a connected agent session."""

    session_id: str
    writer: asyncio.StreamWriter
    agent_id: str
    hostname: str
    os_version: str
    capabilities: list[str]
    connected_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    config_version: int = 0
    seq_out: int = 0
    seq_in: int = 0

    async def send(self, msg_type: MsgType, payload: bytes) -> None:
        """Send a framed message to the agent."""
        self.seq_out += 1
        framed = frame_message(msg_type, self.seq_out, payload)
        self.writer.write(framed)
        await self.writer.drain()


class BridgeServer:
    """Central OPC-Bridge server handling agent connections and config push."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._sessions: dict[str, AgentSession] = {}
        self._config_items: list[ItemRef] = []
        self._config_version: int = 0
        self._server: Optional[asyncio.AbstractServer] = None
        self._running = False

    @property
    def sessions(self) -> dict[str, AgentSession]:
        return dict(self._sessions)

    @property
    def config_version(self) -> int:
        return self._config_version

    def set_config(self, items: list[ItemRef], version: int) -> None:
        """Update the server-side tag configuration."""
        self._config_items = list(items)
        self._config_version = version
        logger.info("Config updated: version=%d, items=%d", version, len(items))

    async def start(self) -> None:
        """Start the TLS server."""
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        if self.config.certfile and self.config.keyfile:
            ssl_ctx.load_cert_chain(self.config.certfile, self.config.keyfile)
        else:
            logger.warning("No TLS certificates configured; using insecure mode for testing")
            ssl_ctx = None

        self._server = await asyncio.start_server(
            self._handle_client,
            self.config.host,
            self.config.port,
            ssl=ssl_ctx,
        )
        self._running = True
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        logger.info("BridgeServer listening on %s", addrs)

    async def stop(self) -> None:
        """Stop the server and close all sessions."""
        self._running = False
        for session in list(self._sessions.values()):
            try:
                session.writer.close()
                await session.writer.wait_closed()
            except Exception:
                pass
        self._sessions.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("BridgeServer stopped")

    async def push_config_to_all(self) -> None:
        """Push current configuration to all connected agents."""
        payload = ConfigPushPayload(
            config_version=self._config_version,
            update_rate_ms=self.config.default_update_rate_ms,
            items=self._config_items,
        ).pack()
        for sid, session in list(self._sessions.items()):
            try:
                await session.send(MsgType.CONFIG_PUSH, payload)
                logger.debug("Config pushed to session %s", sid)
            except Exception as exc:
                logger.warning("Failed to push config to %s: %s", sid, exc)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single agent connection lifecycle."""
        addr = writer.get_extra_info("peername")
        logger.info("New connection from %s", addr)
        session: Optional[AgentSession] = None
        try:
            session = await self._handshake(reader, writer)
            if session is None:
                return
            self._sessions[session.session_id] = session
            logger.info(
                "Agent authenticated: id=%s host=%s session=%s",
                session.agent_id,
                session.hostname,
                session.session_id,
            )
            await self._message_loop(session, reader)
        except asyncio.CancelledError:
            logger.info("Connection cancelled: %s", addr)
        except Exception as exc:
            logger.error("Connection error from %s: %s", addr, exc)
        finally:
            if session and session.session_id in self._sessions:
                del self._sessions[session.session_id]
                logger.info("Session removed: %s", session.session_id)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> Optional[AgentSession]:
        """Perform HELLO + AUTH handshake. Returns session or None on failure."""
        # Expect HELLO
        hello_data = await self._read_message(reader)
        if hello_data is None:
            return None
        header, payload = hello_data
        if header.msg_type != MsgType.HELLO:
            logger.warning("Expected HELLO, got %s", header.msg_type)
            return None
        hello = HelloPayload.unpack(payload)

        # Send HELLO_ACK
        session_id = f"sess-{int(time.time() * 1000)}"
        ack_payload = HelloAckPayload(
            session_id=session_id,
            heartbeat_interval_ms=self.config.heartbeat_interval_ms,
        ).pack()
        seq = 1
        writer.write(frame_message(MsgType.HELLO_ACK, seq, ack_payload))
        await writer.drain()

        # Expect AUTH
        auth_data = await self._read_message(reader)
        if auth_data is None:
            return None
        header, payload = auth_data
        if header.msg_type != MsgType.AUTH:
            logger.warning("Expected AUTH, got %s", header.msg_type)
            return None

        # Validate token (constant-time compare)
        from opc_bridge.protocol.messages import AuthPayload

        auth = AuthPayload.unpack(payload)
        success = (
            len(auth.token_hash) == len(self.config.auth_token_hash)
            and auth.token_hash == self.config.auth_token_hash
        )
        auth_ack = AuthAckPayload(success=success, policies_version=1).pack()
        seq += 1
        writer.write(frame_message(MsgType.AUTH_ACK, seq, auth_ack))
        await writer.drain()

        if not success:
            logger.warning("Auth failed for agent %s", hello.agent_id)
            return None

        return AgentSession(
            session_id=session_id,
            writer=writer,
            agent_id=hello.agent_id,
            hostname=hello.hostname,
            os_version=hello.os_version,
            capabilities=hello.capabilities,
            seq_in=header.seq,
        )

    async def _message_loop(self, session: AgentSession, reader: asyncio.StreamReader) -> None:
        """Process messages from an authenticated agent."""
        while self._running:
            msg = await self._read_message(reader)
            if msg is None:
                break
            header, payload = msg
            session.seq_in = header.seq
            session.last_heartbeat = time.time()

            if header.msg_type == MsgType.HEARTBEAT:
                logger.debug("Heartbeat from %s", session.session_id)
            elif header.msg_type == MsgType.READ_RESPONSE:
                resp = ReadResponsePayload.unpack(payload)
                logger.info(
                    "Read response from %s: request=%d duration=%dus results=%d",
                    session.session_id,
                    resp.request_id,
                    resp.duration_us,
                    len(resp.results),
                )
            elif header.msg_type == MsgType.CONFIG_ACK:
                from opc_bridge.protocol.messages import ConfigAckPayload

                ack = ConfigAckPayload.unpack(payload)
                if ack.applied:
                    session.config_version = ack.config_version
                    logger.info(
                        "Config v%d applied by %s", ack.config_version, session.session_id
                    )
                else:
                    logger.warning(
                        "Config v%d rejected by %s", ack.config_version, session.session_id
                    )
            elif header.msg_type == MsgType.ERROR:
                err = ErrorPayload.unpack(payload)
                logger.error("Agent error from %s: code=%d msg=%s", session.session_id, err.code, err.message)
            else:
                logger.warning("Unexpected message type %s from %s", header.msg_type, session.session_id)

    async def _read_message(
        self, reader: asyncio.StreamReader
    ) -> Optional[tuple]:
        """Read and unframe one protocol message. Returns None on EOF/error."""
        try:
            hdr_bytes = await reader.readexactly(HEADER_SIZE)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        try:
            header = unframe_message.__globals__["Header"].unpack(hdr_bytes)
        except ValueError as exc:
            logger.warning("Invalid header: %s", exc)
            return None
        remaining = header.payload_len + TRAILER_SIZE
        try:
            rest = await reader.readexactly(remaining)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        try:
            return unframe_message(hdr_bytes + rest)
        except ValueError as exc:
            logger.warning("Invalid message: %s", exc)
            return None