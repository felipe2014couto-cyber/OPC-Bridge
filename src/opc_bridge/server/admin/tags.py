"""Safe tag administration projections and short-lived validation approvals."""
from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import uuid
from pathlib import Path

from opc_bridge.protocol.inspection import (
    MAX_TAGS,
    InspectionRequest,
    InspectionResponse,
    valid_text,
)


def read_json(environ: dict) -> dict:
    if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("invalid_json")
    length = int(environ.get("CONTENT_LENGTH") or "0")
    if not 1 <= length <= 65536:
        raise ValueError("invalid_json")
    raw = environ["wsgi.input"].read(length)
    if len(raw) != length:
        raise ValueError("invalid_json")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("invalid_json")
            result[key] = value
        return result

    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(data, dict):
        raise TypeError("invalid_configuration")
    return data


def tag_plan(data: dict, applying: bool = False) -> dict:
    fields = {"opc_prog_id", "update_rate_ms", "tags"}
    if applying:
        fields |= {"validation_id", "confirmed"}
    if set(data) != fields:
        raise ValueError("invalid_configuration")
    rate, prog_id, tags = data.get("update_rate_ms"), data.get("opc_prog_id"), data.get("tags")
    if (type(rate) is not int or not 1000 <= rate <= 60000 or not valid_text(prog_id, 256)
            or not isinstance(tags, list) or not 1 <= len(tags) <= MAX_TAGS
            or any(not valid_text(path) for path in tags) or len(set(tags)) != len(tags)):
        raise ValueError("invalid_configuration")
    return {"opc_prog_id": prog_id, "update_rate_ms": rate, "tags": sorted(tags)}


def fingerprint(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()


class TagAdministration:
    """Mixin using the existing repositories and config operation dispatcher."""

    def init_tag_ui(self) -> None:
        self._validation_tickets = {}
        self._ticket_lock = threading.Lock()

    def ui_asset(self, path, environ, start_response):
        files = {"/ui": ("index.html", "text/html"),
                 "/ui/app.js": ("app.js", "text/javascript"),
                 "/ui/style.css": ("style.css", "text/css")}
        if path not in files:
            return None
        if environ.get("REQUEST_METHOD", "GET") != "GET":
            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
        filename, mime = files[path]
        body = (Path(__file__).parent / "static" / filename).read_bytes()
        # Browsers render this static login shell on 401; no operational data is embedded.
        status = "401 Unauthorized" if path == "/ui" and not self._authorized(environ) else "200 OK"
        start_response(status, [
            ("Content-Type", mime + "; charset=utf-8"), ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", ("default-src 'none'; script-src 'self'; style-src 'self'; "
             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")),
        ])
        return [body]

    def _session_context(self, agent_id):
        if self._bridge_server is None:
            raise ValueError("runtime_unavailable")
        session = next((s for s in list(self._bridge_server._sessions.values())
                        if s.agent_id == agent_id), None)
        if session is None:
            raise ValueError("agent_disconnected")
        return session.session_id, session.config_version

    def _audit_inspection(self, agent_id, action, count, outcome):
        with self._database.session() as repo:
            repo.add_audit_event(agent_id, str(uuid.uuid4()), "inspection." + action,
                                 json.dumps({"item_count": count, "outcome": outcome}))

    def tag_route(self, method, path, query, environ, start_response):
        if path == "/api/v1/ui-capabilities" and method == "GET":
            return self._json_response(start_response, "200 OK", {
                "dispatch_available": self._bridge_server is not None})
        prefix = "/api/v1/agents/"
        if not path.startswith(prefix):
            return None
        parts = path[len(prefix):].split("/")
        if len(parts) != 2 or parts[1] not in {
                "active-config", "opc-servers", "tag-validations", "tag-config-operations"}:
            return None
        agent_id, action = parts
        expected = "GET" if action in ("active-config", "opc-servers") else "POST"
        if method != expected:
            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
        if not agent_id or query:
            return self._json_response(start_response, "400 Bad Request", {"error": "invalid_request"})
        with self._database.session() as repo:
            agent = repo.get_agent(agent_id)
        if agent is None:
            return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
        if action == "active-config":
            with self._database.session() as repo:
                snapshot = repo.latest_applied_snapshot(agent_id)
            configuration = None
            if snapshot is not None:
                stored = json.loads(snapshot.payload_json)
                configuration = {"version": snapshot.version, "opc_prog_id": stored["opc_prog_id"],
                                 "update_rate_ms": stored["update_rate_ms"],
                                 "tags": [item["opc_item_path"] for item in stored["items"]]}
            return self._json_response(start_response, "200 OK", {"configuration": configuration})
        if self._bridge_server is None:
            return self._json_response(start_response, "405 Method Not Allowed", {"error": "read_only_runtime"})
        if not agent.enabled:
            return self._json_response(start_response, "409 Conflict", {"error": "agent_disabled"})
        try:
            data = read_json(environ) if method == "POST" else {}
            plan = tag_plan(data, action == "tag-config-operations") if method == "POST" else None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return self._json_response(start_response, "400 Bad Request", {"error": "invalid_configuration"})
        try:
            context = self._session_context(agent_id)
            if action == "tag-config-operations":
                if data.get("confirmed") is not True:
                    return self._json_response(start_response, "400 Bad Request", {"error": "confirmation_required"})
                ticket_id = data.get("validation_id")
                if not isinstance(ticket_id, str):
                    raise ValueError("validation_required")
                with self._ticket_lock:
                    ticket = self._validation_tickets.pop(ticket_id, None)
                if (ticket is None or ticket[:3] != (agent_id, context, fingerprint(plan))
                        or ticket[3] <= time.monotonic()):
                    raise ValueError("validation_required")
                body = json.dumps({"opc_prog_id": plan["opc_prog_id"],
                                   "update_rate_ms": plan["update_rate_ms"],
                                   "items": [{"item_id": index + 1, "opc_item_path": path,
                                              "requested_source": 0}
                                             for index, path in enumerate(plan["tags"])]}).encode("utf-8")
                operation_environ = dict(environ, CONTENT_LENGTH=str(len(body)),
                                         **{"wsgi.input": io.BytesIO(body)})
                operation_environ["opc_bridge.validated_context"] = context
                return self._create_config_operation(agent_id, operation_environ, start_response)
            request = InspectionRequest(str(uuid.uuid4()), "servers" if action == "opc-servers" else "tags",
                                        plan["opc_prog_id"] if plan else "", plan["tags"] if plan else None)
            response = self._bridge_server.inspect_agent_threadsafe(agent_id, request)
            response = InspectionResponse.unpack(response.pack())
            if response.request_id != request.request_id or self._session_context(agent_id) != context:
                raise ValueError("inspection_failed")
            outcome = response.error or (
                "invalid" if response.results and any(r["status"] != "valid" for r in response.results)
                else "complete")
            self._audit_inspection(agent_id, request.action, len(request.tags or []), outcome)
            if response.error and (action == "opc-servers" or not response.results):
                raise ValueError(response.error)
            if action == "opc-servers":
                return self._json_response(start_response, "200 OK", {"servers": response.servers})
            if response.results is None or [r["opc_item_path"] for r in response.results] != plan["tags"]:
                raise ValueError("inspection_failed")
            success = response.error is None and all(r["status"] == "valid" for r in response.results)
            validation_id = None
            if success:
                validation_id = str(uuid.uuid4())
                with self._ticket_lock:
                    self._validation_tickets = {k: v for k, v in self._validation_tickets.items()
                                                if v[3] > time.monotonic()}
                    if len(self._validation_tickets) >= 100:
                        raise ValueError("inspection_busy")
                    self._validation_tickets[validation_id] = (
                        agent_id, context, fingerprint(plan), time.monotonic() + 300)
            return self._json_response(start_response, "200 OK", {
                "validation_id": validation_id, "valid": success, "results": response.results,
                "expires_in_seconds": 300 if success else 0})
        except (ValueError, TypeError, ConnectionError, TimeoutError) as exc:
            errors = {"agent_disconnected", "inspection_unsupported", "inspection_busy", "runtime_unavailable",
                      "validation_required", "inspection_failed", "unavailable", "busy", "inspection_timeout",
                      "timeout"}
            error = "inspection_timeout" if isinstance(exc, TimeoutError) else (
                str(exc) if str(exc) in errors else "inspection_failed")
            self._audit_inspection(agent_id, action, len(plan["tags"]) if plan else 0, error)
            status = "504 Gateway Timeout" if error in {"inspection_timeout", "timeout"} else "409 Conflict"
            return self._json_response(start_response, status, {"error": error})
