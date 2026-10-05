"""OPC-Bridge central server core: TLS endpoint, session and config management."""
from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import json
import logging
import os
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from opc_bridge.protocol import (
    HEADER_SIZE,
    TRAILER_SIZE,
    AuthAckPayload,
    ConfigPushPayload,
    ErrorPayload,
    HelloAckPayload,
    HelloPayload,
    ItemRef,
    ItemStatus,
    MsgType,
    ReadRequestPayload,
    ReadResponsePayload,
    decode_value,
    format_opc_timestamp,
    format_quality_text,
    frame_message,
    sanitize_error,
    unframe_message,
)
from opc_bridge.protocol.inspection import CAPABILITY, InspectionRequest, InspectionResponse
from opc_bridge.server.credentials import verify_agent_credential_hash
from opc_bridge.server.persistence import Database

logger = logging.getLogger(__name__)



def format_received_at(timestamp_s: float) -> str | None:
    if timestamp_s <= 0:
        return None
    try:
        dt = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except Exception:
        return None


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
    read_cycle_timeout_ms: int = 5000
    opc_prog_id: str = ""
    config_ack_timeout_ms: int = 5000
    persistence: Database | None = None


@dataclass
class ReadSchedulerMetrics:
    """Aggregate metrics for centrally scheduled read cycles."""

    cycles: int = 0
    timeouts: int = 0
    overruns: int = 0
    duration_total_ms: float = 0.0
    duration_max_ms: float = 0.0
    duration_last_ms: float = 0.0


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
    config_items: list[ItemRef] = field(default_factory=list)
    update_rate_ms: int = 1000
    opc_prog_id: str = ""

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
        self._server: asyncio.AbstractServer | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._admin_httpd = None
        self._admin_thread: threading.Thread | None = None
        self._running = False
        self._next_request_id = 0
        self._pending_reads: dict[tuple[str, int], asyncio.Future[ReadResponsePayload]] = {}
        self._scheduler_tasks: dict[str, asyncio.Task[None]] = {}
        self.read_metrics = ReadSchedulerMetrics()
        self._pending_config_operations: dict[tuple[str, int], str] = {}
        self._config_timeout_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._pending_config_payloads: dict[tuple[str, int], ConfigPushPayload] = {}
        self._pending_inspections: dict[tuple[str, str], asyncio.Future] = {}
        # Ephemeral in-memory live values cache: agent_id -> {item_id -> dict}
        # Strictly volatile in memory; never written to persistence or logs.
        self._live_values: dict[str, dict[int, dict[str, Any]]] = {}
        self._live_lock = threading.Lock()
        if self.config.persistence is not None:
            self._recover_persisted_state()

    def _recover_persisted_state(self) -> None:
        """Restore the latest desired snapshot and close state lost in a server crash."""
        assert self.config.persistence is not None
        with self.config.persistence.session() as repo:
            repo.recover_interrupted_sessions()
            for operation in repo.pending_operations():
                repo.complete_operation(operation.operation_id, "expired")
                repo.add_audit_event(
                    operation.agent_id,
                    str(uuid.uuid4()),
                    "config.expired",
                    json.dumps({"operation_id": operation.operation_id, "reason": "server_restart"}),
                )
            snapshot = repo.latest_snapshot()
        if snapshot is None:
            return
        try:
            restored = json.loads(snapshot.payload_json)
            items = [ItemRef(**item) for item in restored["items"]]
            version = int(restored["config_version"])
            update_rate = int(restored["update_rate_ms"])
            prog_id = str(restored["opc_prog_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.exception("Persisted configuration snapshot %s is invalid", snapshot.snapshot_id)
            return
        self._config_items = items
        self._config_version = version
        self.config.default_update_rate_ms = update_rate
        self.config.opc_prog_id = prog_id

    def _persist_config_operation(self, session: AgentSession) -> str | None:
        if self.config.persistence is None:
            return None
        snapshot_id = str(uuid.uuid4())
        operation_id = str(uuid.uuid4())
        payload_json = json.dumps(
            {
                "config_version": self._config_version,
                "update_rate_ms": self.config.default_update_rate_ms,
                "opc_prog_id": self.config.opc_prog_id,
                "items": [
                    {
                        "item_id": item.item_id,
                        "opc_item_path": item.opc_item_path,
                        "requested_source": item.requested_source,
                    }
                    for item in self._config_items
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.config.persistence.session() as repo:
            snapshot = repo.add_snapshot_if_absent(
                session.agent_id, snapshot_id, self._config_version, payload_json
            )
            repo.add_operation(session.agent_id, operation_id, snapshot.snapshot_id)
            repo.add_audit_event(
                session.agent_id,
                str(uuid.uuid4()),
                "config.requested",
                json.dumps({"operation_id": operation_id, "version": self._config_version}),
            )
        return operation_id

    def _complete_config_operation(self, operation_id: str, agent_id: str, status: str) -> None:
        if self.config.persistence is None:
            return
        with self.config.persistence.session() as repo:
            repo.complete_operation(operation_id, status)
            repo.add_audit_event(
                agent_id,
                str(uuid.uuid4()),
                "config." + status,
                json.dumps({"operation_id": operation_id}),
            )

    async def _expire_config_operation(
        self, key: tuple[str, int], operation_id: str, agent_id: str
    ) -> None:
        await asyncio.sleep(max(self.config.config_ack_timeout_ms, 1) / 1000)
        if self._pending_config_operations.get(key) != operation_id:
            return
        self._pending_config_operations.pop(key, None)
        self._pending_config_payloads.pop(key, None)
        self._config_timeout_tasks.pop(key, None)
        self._complete_config_operation(operation_id, agent_id, "expired")

    def _persist_observed_state(
        self, session: AgentSession, state: str, message_type: str, heartbeat: bool = False
    ) -> None:
        if self.config.persistence is None:
            return
        with self.config.persistence.session() as repo:
            repo.update_session_observed(
                session.session_id,
                state,
                json.dumps(
                    {
                        "connection": state,
                        "last_message": message_type,
                        "hostname": session.hostname,
                        "os_version": session.os_version,
                        "applied_config_version": session.config_version,
                    },
                    sort_keys=True,
                ),
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if heartbeat else None,
            )

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
        for session in self._sessions.values():
            session.config_items = list(items)
            session.update_rate_ms = self.config.default_update_rate_ms
            session.opc_prog_id = self.config.opc_prog_id
        logger.info("Config updated: version=%d, items=%d", version, len(items))

    def _allocate_request_id(self) -> int:
        """Return a request ID that is not currently awaiting a response."""
        for _ in range(len(self._pending_reads) + 1):
            request_id = self._next_request_id
            self._next_request_id = (request_id + 1) & 0xFFFFFFFF
            if all(key[1] != request_id for key in self._pending_reads):
                return request_id
        raise RuntimeError("No request IDs available")

    async def start(self) -> None:
        """Start the TLS server."""
        self._event_loop = asyncio.get_running_loop()
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
        try:
            self._start_admin_api()
        except Exception:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            raise
        self._running = True
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        logger.info("BridgeServer listening on %s", addrs)

    def _start_admin_api(self) -> None:
        """Embed the loopback admin API in this persisted BridgeServer runtime.

        Setting ADMIN_API_TOKEN enables the writable API. The API receives this
        exact BridgeServer and its Database instance; without the token, no
        administrative HTTP listener is started.
        """
        if not os.environ.get("ADMIN_API_TOKEN"):
            return
        if self.config.persistence is None:
            raise RuntimeError("The admin API requires BridgeServer persistence")
        port_text = os.environ.get("ADMIN_API_PORT", "8081")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise RuntimeError("ADMIN_API_PORT must be an integer") from exc
        if not 0 <= port <= 65535:
            raise RuntimeError("ADMIN_API_PORT must be between 0 and 65535")
        from wsgiref.simple_server import make_server

        from opc_bridge.server.admin import create_app

        app = create_app(self.config.persistence, self)
        self._admin_httpd = make_server("127.0.0.1", port, app)
        self._admin_thread = threading.Thread(
            target=self._admin_httpd.serve_forever,
            name="opc-bridge-admin-api",
            daemon=True,
        )
        self._admin_thread.start()
        logger.info("Admin API listening on 127.0.0.1:%d", self._admin_httpd.server_port)

    async def stop(self) -> None:
        """Stop the server and close all sessions."""
        self._running = False
        if self._admin_httpd is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._admin_httpd.shutdown)
            self._admin_httpd.server_close()
            if self._admin_thread is not None:
                await loop.run_in_executor(None, self._admin_thread.join)
            self._admin_httpd = None
            self._admin_thread = None
        for task in list(self._scheduler_tasks.values()):
            task.cancel()
        if self._scheduler_tasks:
            await asyncio.gather(*self._scheduler_tasks.values(), return_exceptions=True)
        self._scheduler_tasks.clear()
        for future in self._pending_reads.values():
            if not future.done():
                future.cancel()
        self._pending_reads.clear()
        for future in self._pending_inspections.values():
            if not future.done():
                future.cancel()
        self._pending_inspections.clear()
        for session in list(self._sessions.values()):
            try:
                session.writer.close()
                await session.writer.wait_closed()
            except (OSError, ConnectionError) as exc:
                logger.debug("Error closing session %s: %s", session.session_id, exc)
        for key, operation_id in list(self._pending_config_operations.items()):
            session = next(
                (active for active in self._sessions.values() if active.session_id == key[0]),
                None,
            )
            if session is not None:
                self._complete_config_operation(operation_id, session.agent_id, "expired")
            timeout_task = self._config_timeout_tasks.pop(key, None)
            if timeout_task is not None:
                timeout_task.cancel()
        self._pending_config_operations.clear()
        self._pending_config_payloads.clear()
        with self._live_lock:
            self._live_values.clear()
        self._sessions.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("BridgeServer stopped")

    async def push_config_to_all(self) -> None:
        """Push current configuration to all connected agents."""
        for sid, session in list(self._sessions.items()):
            operation_id = None
            try:
                operation_id = self._persist_config_operation(session)
                config_payload = ConfigPushPayload(
                    self._config_version,
                    self.config.default_update_rate_ms,
                    list(self._config_items),
                    self.config.opc_prog_id,
                )
                await self._send_config_payload(session, config_payload, operation_id)
            except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                logger.warning("Failed to push config to %s: %s", sid, exc)

    async def _send_config_payload(
        self,
        session: AgentSession,
        config_payload: ConfigPushPayload,
        operation_id: str | None,
    ) -> bool:
        key = (session.session_id, config_payload.config_version)
        if key in self._pending_config_operations:
            return False
        if operation_id is not None:
            self._pending_config_operations[key] = operation_id
            self._pending_config_payloads[key] = config_payload
            self._config_timeout_tasks[key] = asyncio.create_task(
                self._expire_config_operation(key, operation_id, session.agent_id),
                name=f"config-ack-timeout-{session.session_id}-{config_payload.config_version}",
            )
        try:
            await session.send(MsgType.CONFIG_PUSH, config_payload.pack())
        except (OSError, ConnectionError, asyncio.TimeoutError):
            self._pending_config_operations.pop(key, None)
            self._pending_config_payloads.pop(key, None)
            timeout_task = self._config_timeout_tasks.pop(key, None)
            if timeout_task is not None:
                timeout_task.cancel()
            if operation_id is not None:
                self._complete_config_operation(operation_id, session.agent_id, "failed")
            raise
        logger.debug("Config pushed to session %s", session.session_id)
        return True

    async def dispatch_admin_config_operation(
        self, agent_id: str, operation_id: str, payload: ConfigPushPayload,
        expected_context: tuple[str, int] | None = None,
    ) -> bool | None:
        """Send a persisted admin operation through the existing CONFIG_PUSH path."""
        session = next(
            (active for active in self._sessions.values() if active.agent_id == agent_id),
            None,
        )
        if expected_context is not None and (
                session is None or (session.session_id, session.config_version) != expected_context):
            return False
        if session is None:
            return None
        return await self._send_config_payload(session, payload, operation_id)

    async def inspect_agent(self, agent_id: str, request: InspectionRequest) -> InspectionResponse:
        """Correlate inspection with the current authenticated session only."""
        session = next((s for s in self._sessions.values() if s.agent_id == agent_id), None)
        if session is None:
            raise ValueError("agent_disconnected")
        if CAPABILITY not in session.capabilities:
            raise ValueError("inspection_unsupported")
        if any(sid == session.session_id for sid, _ in self._pending_inspections):
            raise ValueError("inspection_busy")
        key = (session.session_id, request.request_id)
        future = asyncio.get_running_loop().create_future()
        self._pending_inspections[key] = future
        try:
            await session.send(MsgType.OPC_INSPECT_REQUEST, request.pack())
            response = await asyncio.wait_for(future, timeout=45)
            if (request.action == "tags" and response.error is None and
                    (response.servers is not None or response.results is None or
                     [r["opc_item_path"] for r in response.results] != request.tags)):
                raise ValueError("inspection_failed")
            if request.action == "servers" and response.error is None and response.servers is None:
                raise ValueError("inspection_failed")
            return response
        finally:
            self._pending_inspections.pop(key, None)

    def inspect_agent_threadsafe(self, agent_id: str, request: InspectionRequest) -> InspectionResponse:
        loop = self._event_loop
        if loop is None or not loop.is_running():
            raise ValueError("runtime_unavailable")
        future = asyncio.run_coroutine_threadsafe(self.inspect_agent(agent_id, request), loop)
        try:
            return future.result(timeout=47)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise ValueError("inspection_timeout") from None

    def dispatch_admin_config_operation_threadsafe(
        self, agent_id: str, operation_id: str, payload: ConfigPushPayload,
        expected_context: tuple[str, int] | None = None,
    ) -> bool | None:
        """Bridge the WSGI worker thread to the running server event loop."""
        loop = self._event_loop
        if loop is None or not loop.is_running():
            return False if expected_context is not None else None
        future = asyncio.run_coroutine_threadsafe(
            self.dispatch_admin_config_operation(agent_id, operation_id, payload, expected_context), loop
        )
        try:
            return future.result(timeout=max(1, self.config.read_cycle_timeout_ms / 1000))
        except concurrent.futures.TimeoutError:
            future.cancel()
            return None

    def _record_live_values(self, session: AgentSession, resp: ReadResponsePayload) -> None:
        """Store the latest read results in volatile memory only. Never logged or persisted."""
        now_wall = time.time()
        now_mono = time.monotonic()
        items_by_id = {item.item_id: item.opc_item_path for item in session.config_items}
        with self._live_lock:
            agent_cache = self._live_values.setdefault(session.agent_id, {})
            for res in resp.results:
                opc_path = items_by_id.get(res.item_id, "")
                val, val_type_name = decode_value(res.value_type, res.value)
                err_str = sanitize_error(res.error_code, res.status)
                agent_cache[res.item_id] = {
                    "agent_id": session.agent_id,
                    "item_id": res.item_id,
                    "opc_item_path": opc_path,
                    "value": val,
                    "value_type": val_type_name,
                    "quality": res.quality,
                    "quality_text": format_quality_text(res.quality),
                    "opc_timestamp": format_opc_timestamp(res.timestamp_us),
                    "received_at": format_received_at(now_wall),
                    "received_at_mono": now_mono,
                    "error": err_str,
                }

    def clear_live_values(self, agent_id: str) -> None:
        """Purge volatile live values for an agent upon session disconnect or config wipe."""
        with self._live_lock:
            self._live_values.pop(agent_id, None)

    def get_live_values(self, agent_id: str) -> dict[str, Any]:
        """Threadsafe lookup of ephemeral live values for an agent's active configuration."""
        session = next((s for s in self._sessions.values() if s.agent_id == agent_id), None)
        if session is None:
            return {
                "agent_id": agent_id,
                "connected": False,
                "config_version": None,
                "update_rate_ms": None,
                "status": "agent_disconnected",
                "items": [],
            }
        if not session.config_items or session.config_version == 0:
            return {
                "agent_id": agent_id,
                "connected": True,
                "config_version": session.config_version,
                "update_rate_ms": session.update_rate_ms,
                "status": "no_active_configuration",
                "items": [],
            }

        now_mono = time.monotonic()
        # TTL rule: 3 * update_rate_ms, minimum 15000 ms (15s)
        ttl_ms = max(3 * session.update_rate_ms, 15000)
        items_result = []

        with self._live_lock:
            agent_cache = self._live_values.get(agent_id, {})
            for item in session.config_items:
                cached = agent_cache.get(item.item_id)
                if cached is None:
                    items_result.append({
                        "item_id": item.item_id,
                        "opc_item_path": item.opc_item_path,
                        "available": False,
                        "status": "waiting_first_read",
                        "value": None,
                        "value_type": None,
                        "quality": None,
                        "quality_text": None,
                        "opc_timestamp": None,
                        "received_at": None,
                        "age_ms": None,
                        "stale": False,
                        "error": None,
                    })
                else:
                    age_ms = int((now_mono - cached["received_at_mono"]) * 1000)
                    is_stale = age_ms > ttl_ms
                    has_error = bool(cached["error"])
                    if is_stale:
                        status = "stale"
                    elif has_error:
                        status = "error"
                    else:
                        status = "ok"
                    items_result.append({
                        "item_id": item.item_id,
                        "opc_item_path": item.opc_item_path,
                        "available": True,
                        "status": status,
                        "value": cached["value"],
                        "value_type": cached["value_type"],
                        "quality": cached["quality"],
                        "quality_text": cached["quality_text"],
                        "opc_timestamp": cached["opc_timestamp"],
                        "received_at": cached["received_at"],
                        "age_ms": age_ms,
                        "stale": is_stale,
                        "error": cached["error"],
                    })

        return {
            "agent_id": agent_id,
            "connected": True,
            "config_version": session.config_version,
            "update_rate_ms": session.update_rate_ms,
            "status": "ok",
            "items": items_result,
        }


    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single agent connection lifecycle."""
        addr = writer.get_extra_info("peername")
        logger.info("New connection from %s", addr)
        session: AgentSession | None = None
        try:
            session = await self._handshake(reader, writer)
            if session is None:
                return
            self._sessions[session.session_id] = session
            scheduler_task = asyncio.create_task(
                self._schedule_reads(session),
                name=f"read-scheduler-{session.session_id}",
            )
            self._scheduler_tasks[session.session_id] = scheduler_task
            logger.info(
                "Agent authenticated: id=%s host=%s session=%s",
                session.agent_id,
                session.hostname,
                session.session_id,
            )
            await self._message_loop(session, reader)
        except asyncio.CancelledError:
            logger.info("Connection cancelled: %s", addr)
        except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
            logger.error("Connection error from %s: %s", addr, exc)
        finally:
            scheduler_task = self._scheduler_tasks.pop(session.session_id, None) if session else None
            if scheduler_task:
                scheduler_task.cancel()
                await asyncio.gather(scheduler_task, return_exceptions=True)
            if session:
                for key, future in list(self._pending_inspections.items()):
                    if key[0] == session.session_id and not future.done():
                        future.set_exception(ConnectionError("Agent disconnected"))
                for key, future in list(self._pending_reads.items()):
                    if key[0] == session.session_id:
                        self._pending_reads.pop(key, None)
                        if not future.done():
                            future.cancel()
            if session:
                self.clear_live_values(session.agent_id)
            if session and session.session_id in self._sessions:
                del self._sessions[session.session_id]
                logger.info("Session removed: %s", session.session_id)
            if session and self.config.persistence is not None:
                with self.config.persistence.session() as repo:
                    repo.disconnect_session(
                        session.session_id,
                        json.dumps(
                            {
                                "connection": "disconnected",
                                "hostname": session.hostname,
                                "os_version": session.os_version,
                                "applied_config_version": session.config_version,
                            },
                            sort_keys=True,
                        ),
                    )
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, ConnectionError) as exc:
                logger.debug("Error closing writer for %s: %s", addr, exc)

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> AgentSession | None:
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
        session_id = "sess-" + str(uuid.uuid4())
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
        applied_version: int | None = None
        session_items = list(self._config_items)
        session_update_rate_ms = self.config.default_update_rate_ms
        session_opc_prog_id = self.config.opc_prog_id
        if self.config.persistence is None:
            success = hmac.compare_digest(auth.token_hash, self.config.auth_token_hash)
        else:
            success = False
            with self.config.persistence.session() as repo:
                agent = repo.get_agent(hello.agent_id)
                if agent is not None and agent.enabled:
                    for stored_hash in repo.active_credential_hashes(hello.agent_id):
                        success = verify_agent_credential_hash(auth.token_hash, stored_hash) or success
                    previous_snapshot = repo.latest_applied_snapshot(hello.agent_id)
                    if previous_snapshot is not None:
                        try:
                            previous_config = json.loads(previous_snapshot.payload_json)
                            applied_version = int(previous_config["config_version"])
                            session_items = [ItemRef(**item) for item in previous_config["items"]]
                            session_update_rate_ms = int(previous_config["update_rate_ms"])
                            session_opc_prog_id = str(previous_config["opc_prog_id"])
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                            logger.exception("Persisted applied config for %s is invalid", hello.agent_id)
        auth_ack = AuthAckPayload(success=success, policies_version=1).pack()
        if not success:
            seq += 1
            writer.write(frame_message(MsgType.AUTH_ACK, seq, auth_ack))
            await writer.drain()
            logger.warning("Auth failed for agent %s", hello.agent_id)
            return None

        if self.config.persistence is not None:
            with self.config.persistence.session() as repo:
                repo.add_session(
                    hello.agent_id,
                    session_id,
                    hello.hostname,
                    hello.os_version,
                    applied_config_version=applied_version,
                )
                repo.update_session_observed(
                    session_id,
                    "connected",
                    json.dumps(
                        {
                            "connection": "connected",
                            "hostname": hello.hostname,
                            "os_version": hello.os_version,
                            "applied_config_version": applied_version,
                        },
                        sort_keys=True,
                    ),
                )

        seq += 1
        writer.write(frame_message(MsgType.AUTH_ACK, seq, auth_ack))
        await writer.drain()

        return AgentSession(
            session_id=session_id,
            writer=writer,
            agent_id=hello.agent_id,
            hostname=hello.hostname,
            os_version=hello.os_version,
            capabilities=hello.capabilities,
            config_version=applied_version or 0,
            seq_in=header.seq,
            config_items=session_items,
            update_rate_ms=session_update_rate_ms,
            opc_prog_id=session_opc_prog_id,
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
            try:
                message_name = MsgType(header.msg_type).name
            except ValueError:
                message_name = str(header.msg_type)
            self._persist_observed_state(session, "connected", message_name)

            if header.msg_type == MsgType.HEARTBEAT:
                logger.debug("Heartbeat from %s", session.session_id)
                self._persist_observed_state(session, "connected", message_name, heartbeat=True)
            elif header.msg_type == MsgType.READ_RESPONSE:
                resp = ReadResponsePayload.unpack(payload)
                pending = self._pending_reads.get((session.session_id, resp.request_id))
                if pending is not None and not pending.done():
                    pending.set_result(resp)
                self._record_live_values(session, resp)
                logger.info(
                    "Read response from %s: request=%d duration=%dus results=%d",
                    session.session_id,
                    resp.request_id,
                    resp.duration_us,
                    len(resp.results),
                )
            elif header.msg_type == MsgType.OPC_INSPECT_RESPONSE:
                try:
                    response = InspectionResponse.unpack(payload)
                except (ValueError, TypeError, UnicodeError):
                    logger.warning("Invalid inspection response from session %s", session.session_id)
                    continue
                future = self._pending_inspections.get((session.session_id, response.request_id))
                if future is not None and not future.done():
                    future.set_result(response)
            elif header.msg_type == MsgType.CONFIG_ACK:
                from opc_bridge.protocol.messages import ConfigAckPayload

                ack = ConfigAckPayload.unpack(payload)
                key = (session.session_id, ack.config_version)
                operation_id = self._pending_config_operations.pop(key, None)
                config_payload = self._pending_config_payloads.pop(key, None)
                timeout_task = self._config_timeout_tasks.pop(key, None)
                if timeout_task is not None:
                    timeout_task.cancel()
                persisted = self.config.persistence is not None
                if ack.applied and (not persisted or operation_id is not None):
                    session.config_version = ack.config_version
                    if config_payload is not None:
                        session.config_items = list(config_payload.items)
                        session.update_rate_ms = config_payload.update_rate_ms
                        session.opc_prog_id = config_payload.opc_prog_id
                        new_item_ids = {item.item_id for item in config_payload.items}
                        with self._live_lock:
                            if session.agent_id in self._live_values:
                                self._live_values[session.agent_id] = {
                                    iid: val for iid, val in self._live_values[session.agent_id].items()
                                    if iid in new_item_ids
                                }
                    if operation_id is not None:
                        self._complete_config_operation(operation_id, session.agent_id, "applied")
                        with self.config.persistence.session() as repo:
                            repo.update_session_applied_version(session.session_id, ack.config_version)
                    logger.info(
                        "Config v%d applied by %s", ack.config_version, session.session_id
                    )
                else:
                    if operation_id is not None:
                        self._complete_config_operation(operation_id, session.agent_id, "rejected")
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
    ) -> tuple | None:
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

    async def _schedule_reads(self, session: AgentSession) -> None:
        """Run one non-overlapping read cycle per configured agent group."""
        interval_ms = max(1, session.update_rate_ms)
        next_tick = time.monotonic() + interval_ms / 1000
        while self._running and session.session_id in self._sessions:
            interval_ms = max(1, session.update_rate_ms)
            interval = interval_ms / 1000
            timeout = max(1, self.config.read_cycle_timeout_ms) / 1000
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if not self._running or session.session_id not in self._sessions:
                return

            started = time.monotonic()
            request_id = self._allocate_request_id()
            future: asyncio.Future[ReadResponsePayload] = asyncio.get_running_loop().create_future()
            key = (session.session_id, request_id)
            self._pending_reads[key] = future
            try:
                request = ReadRequestPayload(request_id=request_id, items=list(session.config_items))
                await session.send(MsgType.READ_REQUEST, request.pack())
                await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                self.read_metrics.timeouts += 1
                logger.warning("Read cycle timed out: session=%s request=%d", session.session_id, request_id)
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError):
                return
            finally:
                self._pending_reads.pop(key, None)
                if not future.done():
                    future.cancel()

            elapsed = time.monotonic() - started
            elapsed_ms = elapsed * 1000
            self.read_metrics.cycles += 1
            self.read_metrics.duration_last_ms = elapsed_ms
            self.read_metrics.duration_total_ms += elapsed_ms
            self.read_metrics.duration_max_ms = max(self.read_metrics.duration_max_ms, elapsed_ms)

            # Keep the cadence anchored to the original schedule. Any ticks
            # elapsed while this cycle ran are dropped instead of queued.
            next_tick += interval
            now = time.monotonic()
            if now >= next_tick:
                skipped = int((now - next_tick) // interval) + 1
                self.read_metrics.overruns += skipped
                logger.warning(
                    "Read cycle overrun: session=%s dropped_ticks=%d",
                    session.session_id,
                    skipped,
                )
                next_tick += skipped * interval
