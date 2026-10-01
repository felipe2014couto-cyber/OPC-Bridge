"""Loopback-only, read-only administrative HTTP API."""
from __future__ import annotations

import hmac
import json
import os
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

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


class AdminApplication:
    """WSGI application with explicit bearer authentication and safe projections."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._admin_token = _configured_admin_token().encode("utf-8")
        self._service_version = _service_version()

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
        self, method: str, path: str, query: str, start_response: Callable[..., Any]
    ) -> list[bytes]:
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
        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})

    def __call__(
        self, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes]:
        if not self._authorized(environ):
            return self._json_response(start_response, "401 Unauthorized", {"error": "unauthorized"})
        try:
            return self._read_request(
                str(environ.get("REQUEST_METHOD", "GET")).upper(),
                str(environ.get("PATH_INFO", "/")),
                str(environ.get("QUERY_STRING", "")),
                start_response,
            )
        except Exception:  # noqa: BLE001 - hide storage details from HTTP responses.
            # Database details can include local paths or connection credentials.
            return self._json_response(
                start_response, "503 Service Unavailable", {"error": "service_unavailable"}
            )


def create_app(database: Database | None = None) -> AdminApplication:
    """Build the admin app; ADMIN_API_TOKEN is mandatory and is read only here."""
    _configured_admin_token()
    store = database if database is not None else database_from_env()
    return AdminApplication(store)


def serve() -> None:
    """Run the API on loopback only; no network interface override is exposed."""
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
