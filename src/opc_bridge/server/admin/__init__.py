"""Loopback-only administrative HTTP API."""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
import uuid
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

from opc_bridge.protocol import ConfigPushPayload, ItemRef
from opc_bridge.server.admin.tags import TagAdministration
from opc_bridge.server.core import BridgeServer
from opc_bridge.server.persistence import Database, database_from_env

SERVICE_NAME = "opc-bridge"
SERVICE_VERSION = "0.1.0"

_SAFE_ERRORS = {
    "pending": None,
    "applied": None,
    "rejected": "Agent rejected the configuration.",
    "expired": "Timed out waiting for agent acknowledgement.",
    "failed": "Configuration delivery failed.",
}


def _service_version() -> str:
    try:
        return version("opc-bridge")
    except PackageNotFoundError:
        return SERVICE_VERSION


def _configured_admin_token() -> str:
    token = os.environ.get("ADMIN_API_TOKEN")
    if not token or token.strip() != token:
        raise RuntimeError("ADMIN_API_TOKEN must be set to a non-empty token")
    return token


def _agent_view(row: dict[str, Any], detailed: bool = False) -> dict[str, Any]:
    state = row["observed_state"] or "unknown"
    observed_state = {
        "connection": state,
        "hostname": row["hostname"] or None,
        "os_version": row["os_version"] or None,
        "applied_config_version": row["applied_config_version"],
        "observed_config_version": row["observed_config_version"],
    }
    current_session = None
    if row["session_id"] is not None and row["session_ended_at"] is None:
        current_session = {
            "session_id": row["session_id"],
            "started_at": row["session_started_at"],
        }
    result = {
        "agent_id": row["agent_id"],
        "state": state,
        "last_heartbeat": row["last_heartbeat_at"],
        "current_session": current_session,
        "observed_state": observed_state,
    }
    if detailed:
        result.update(
            {
                "display_name": row["display_name"],
                "enabled": bool(row["enabled"]),
                "created_at": row["created_at"],
            }
        )
    return result


def _operation_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": row["operation_id"],
        "agent_id": row["agent_id"],
        "status": row["status"],
        "version": row["version"],
        "requested_at": row["requested_at"],
        "completed_at": row["completed_at"],
        "error": _SAFE_ERRORS.get(row["status"], "Operation status unavailable."),
    }


def _same_database(left: Database, right: Database) -> bool:
    if left is right:
        return True
    if left.dialect != right.dialect:
        return False
    if left.dialect == "sqlite":
        return left._sqlite_path == right._sqlite_path
    return left.database_url == right.database_url


class AdminApplication(TagAdministration):
    """WSGI application with explicit bearer authentication and safe projections."""

    def __init__(self, database: Database, bridge_server: BridgeServer | None = None) -> None:
        self._database = database
        self._bridge_server = bridge_server
        self._admin_token = _configured_admin_token().encode("utf-8")
        self._service_version = _service_version()
        self.init_tag_ui()

    def _authorized(self, environ: dict[str, Any]) -> bool:
        authorization = str(environ.get("HTTP_AUTHORIZATION", ""))
        scheme, separator, supplied = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            supplied = ""
        return hmac.compare_digest(supplied.encode("utf-8"), self._admin_token)

    @staticmethod
    def _json_response(
        start_response: Callable[..., Any],
        status: str,
        value: Any,
    ) -> list[bytes]:
        body = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        start_response(
            status,
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
        return [body]

    def _read_request(
        self, method: str, path: str, query: str, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> list[bytes]:
        tag_response = self.tag_route(method, path, query, environ, start_response)
        if tag_response is not None:
            return tag_response
        if (method == "POST" and self._bridge_server is not None and
                path.startswith("/api/v1/agents/") and path.endswith("/config-operations")):
            agent_id = path[len("/api/v1/agents/") : -len("/config-operations")].rstrip("/")
            if not agent_id or "/" in agent_id or query:
                return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
            return self._create_config_operation(agent_id, environ, start_response)
        if method != "GET":
            response = self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
            # WSGI clients can use this header to avoid probing unsupported methods.
            return response
        if path == "/health":
            return self._json_response(
                start_response,
                "200 OK",
                {"status": "ok", "service": SERVICE_NAME, "version": self._service_version},
            )
        if path == "/api/v1/agents":
            with self._database.session() as repo:
                rows = repo.list_admin_agents()
            return self._json_response(
                start_response, "200 OK", {"agents": [_agent_view(row) for row in rows]}
            )
        if path.startswith("/api/v1/agents/"):
            agent_id = path[len("/api/v1/agents/") :]
            if not agent_id or "/" in agent_id:
                return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
            with self._database.session() as repo:
                row = repo.get_admin_agent(agent_id)
            if row is None:
                return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
            return self._json_response(start_response, "200 OK", {"agent": _agent_view(row, True)})
        if path == "/api/v1/config-operations":
            try:
                params = parse_qs(query, keep_blank_values=True, strict_parsing=True) if query else {}
            except ValueError:
                return self._json_response(start_response, "400 Bad Request", {"error": "invalid_query"})
            if set(params) - {"agent_id"} or any(len(values) != 1 for values in params.values()):
                return self._json_response(start_response, "400 Bad Request", {"error": "invalid_query"})
            agent_id = params.get("agent_id", [None])[0]
            if agent_id == "":
                return self._json_response(start_response, "400 Bad Request", {"error": "invalid_query"})
            with self._database.session() as repo:
                rows = repo.list_admin_config_operations(agent_id)
            return self._json_response(
                start_response,
                "200 OK",
                {"operations": [_operation_view(row) for row in rows]},
            )
        if path.startswith("/api/v1/config-operations/") and not query:
            operation_id = path[len("/api/v1/config-operations/"):]
            if not operation_id or "/" in operation_id:
                return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
            with self._database.session() as repo:
                operation = repo.get_admin_config_operation(operation_id)
            if operation is None:
                return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
            return self._json_response(start_response, "200 OK", {"operation": _operation_view(operation)})
        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})

    @staticmethod
    def _parse_configuration(environ: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
            return None, "invalid_content_type"
        try:
            length = int(environ.get("CONTENT_LENGTH") or "")
        except (TypeError, ValueError):
            return None, "invalid_json"
        if length <= 0 or length > 1_048_576:
            return None, "invalid_json"
        try:
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                return None, "invalid_json"

            def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result

            data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None, "invalid_configuration"
        if not isinstance(data, dict) or set(data) - {"update_rate_ms", "opc_prog_id", "items"}:
            return None, "invalid_configuration"
        rate = data.get("update_rate_ms")
        if type(rate) is not int or not 1 <= rate <= 0xFFFFFFFF:
            return None, "invalid_configuration"
        prog_id = data.get("opc_prog_id", "")
        items_data = data.get("items")
        if not isinstance(prog_id, str) or not isinstance(items_data, list) or not items_data:
            return None, "invalid_configuration"
        items: list[ItemRef] = []
        item_ids: set[int] = set()
        paths: set[str] = set()
        if len(items_data) > 0xFFFF:
            return None, "invalid_configuration"
        for item in items_data:
            if not isinstance(item, dict) or set(item) - {"item_id", "opc_item_path", "requested_source"}:
                return None, "invalid_configuration"
            item_id = item.get("item_id")
            path = item.get("opc_item_path")
            source = item.get("requested_source", 0)
            if (type(item_id) is not int or not 0 <= item_id <= 0xFFFFFFFF or
                    not isinstance(path, str) or not path.strip() or type(source) is not int or source != 0):
                return None, "invalid_configuration"
            if item_id in item_ids or path in paths:
                return None, "invalid_configuration"
            item_ids.add(item_id)
            paths.add(path)
            items.append(ItemRef(item_id, path, source))
        config = {"update_rate_ms": rate, "opc_prog_id": prog_id, "items": items}
        try:
            ConfigPushPayload(1, rate, items, prog_id).pack()
        except (OverflowError, UnicodeEncodeError, ValueError):
            return None, "invalid_configuration"
        return config, None

    def _create_config_operation(
        self, agent_id: str, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> list[bytes]:
        if (self._bridge_server is None or self._bridge_server.config.persistence is None or
                not _same_database(self._database, self._bridge_server.config.persistence)):
            return self._json_response(start_response, "503 Service Unavailable", {"error": "service_unavailable"})
        config, error = self._parse_configuration(environ)
        if error:
            status = "415 Unsupported Media Type" if error == "invalid_content_type" else "400 Bad Request"
            return self._json_response(start_response, status, {"error": error})
        assert config is not None
        operation_id = str(uuid.uuid4())
        snapshot_id = str(uuid.uuid4())
        try:
            with self._database.session() as repo:
                agent = repo.get_agent(agent_id)
                if agent is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
                if not agent.enabled:
                    return self._json_response(start_response, "409 Conflict", {"error": "agent_disabled"})
                config_version = repo.next_config_version(agent_id)
                if config_version > 0xFFFFFFFF:
                    return self._json_response(start_response, "409 Conflict", {"error": "version_exhausted"})
                payload_json = json.dumps(
                    {
                        "config_version": config_version,
                        "update_rate_ms": config["update_rate_ms"],
                        "opc_prog_id": config["opc_prog_id"],
                        "items": [
                            {"item_id": item.item_id, "opc_item_path": item.opc_item_path,
                             "requested_source": item.requested_source}
                            for item in config["items"]
                        ],
                    }, sort_keys=True, separators=(",", ":"),
                )
                repo.add_snapshot(agent_id, snapshot_id, config_version, payload_json)
                repo.add_operation(agent_id, operation_id, snapshot_id)
                repo.add_audit_event(
                    agent_id, str(uuid.uuid4()), "config.requested",
                    json.dumps({"operation_id": operation_id, "version": config_version,
                                "item_count": len(config["items"]),
                                "ui_confirmed": environ.get("opc_bridge.validated_context") is not None}),
                )
        except Exception as exc:
            if isinstance(exc, (ValueError, sqlite3.IntegrityError)) or getattr(exc, "pgcode", None) == "23505":
                return self._json_response(start_response, "409 Conflict", {"error": "configuration_conflict"})
            raise
        push = ConfigPushPayload(
            config_version, config["update_rate_ms"], config["items"], config["opc_prog_id"]
        )
        try:
            expected_context = environ.get("opc_bridge.validated_context")
            if expected_context is not None:
                dispatched = self._bridge_server.dispatch_admin_config_operation_threadsafe(
                    agent_id, operation_id, push, expected_context=expected_context
                )
            else:
                dispatched = self._bridge_server.dispatch_admin_config_operation_threadsafe(
                    agent_id, operation_id, push
                )
            if dispatched is False:
                with self._database.session() as repo:
                    repo.complete_operation(operation_id, "failed")
                    repo.add_audit_event(
                        agent_id, str(uuid.uuid4()), "config.conflict",
                        json.dumps({"operation_id": operation_id, "reason": "dispatch_conflict"}),
                    )
                return self._json_response(start_response, "409 Conflict", {"error": "configuration_conflict"})
        except Exception:  # noqa: BLE001 - do not expose transport/storage details.
            return self._json_response(start_response, "503 Service Unavailable", {"error": "service_unavailable"})
        with self._database.session() as repo:
            operation = repo.get_admin_config_operation(operation_id)
        if operation is None:
            return self._json_response(start_response, "503 Service Unavailable", {"error": "service_unavailable"})
        return self._json_response(
            start_response, "201 Created",
            {"operation_id": operation_id, "agent_id": agent_id,
             "version": config_version, "status": operation["status"]},
        )

    def __call__(
        self, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes]:
        asset = self.ui_asset(str(environ.get("PATH_INFO", "/")), environ, start_response)
        if asset is not None:
            return asset
        if not self._authorized(environ):
            return self._json_response(start_response, "401 Unauthorized", {"error": "unauthorized"})
        try:
            return self._read_request(
                str(environ.get("REQUEST_METHOD", "GET")).upper(),
                str(environ.get("PATH_INFO", "/")),
                str(environ.get("QUERY_STRING", "")),
                environ,
                start_response,
            )
        except Exception:  # noqa: BLE001 - hide storage details from HTTP responses.
            # Database details can include local paths or connection credentials.
            return self._json_response(
                start_response, "503 Service Unavailable", {"error": "service_unavailable"}
            )


def create_app(database: Database | None = None, bridge_server: BridgeServer | None = None) -> AdminApplication:
    """Build the admin app; ADMIN_API_TOKEN is mandatory and is read only here."""
    _configured_admin_token()
    store = database if database is not None else database_from_env()
    return AdminApplication(store, bridge_server)


def serve() -> None:
    """Run the standalone read-only API on loopback.

    Writable operations are available only when BridgeServer.start() embeds
    this API in the persisted server runtime. This standalone mode has no
    dispatcher and therefore does not register POST configuration operations.
    """
    port_text = os.environ.get("ADMIN_API_PORT", "8081")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise RuntimeError("ADMIN_API_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("ADMIN_API_PORT must be between 1 and 65535")
    app = create_app()
    with make_server("127.0.0.1", port, app) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    serve()
