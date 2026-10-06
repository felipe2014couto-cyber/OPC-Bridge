"""Safe tag administration projections and short-lived validation approvals."""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs

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


def read_optional_json(environ: dict) -> dict:
    try:
        length = int(environ.get("CONTENT_LENGTH") or "0")
        if length <= 0:
            return {}
        return read_json(environ)
    except Exception:
        return {}


def validate_ip(ip_str: str) -> str:
    if not isinstance(ip_str, str):
        raise ValueError("invalid_ip")
    trimmed = ip_str.strip()
    if not trimmed:
        raise ValueError("invalid_ip")
    try:
        return str(ipaddress.ip_address(trimmed))
    except ValueError:
        raise ValueError("invalid_ip")


def equipment_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("invalid_configuration")
    name = data.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 255:
        raise ValueError("invalid_name")
    ip_str = data.get("ip_address")
    ip_addr = validate_ip(ip_str)
    agent_id = data.get("agent_id")
    if agent_id is not None and (not isinstance(agent_id, str) or not agent_id.strip()):
        agent_id = None
    return {
        "name": name.strip(),
        "ip_address": ip_addr,
        "agent_id": agent_id.strip() if agent_id else None,
    }


def named_config_payload(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("invalid_configuration")
    name = data.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 255:
        raise ValueError("invalid_name")
    eq_id = data.get("equipment_id")
    if not isinstance(eq_id, str) or not eq_id.strip():
        raise ValueError("invalid_equipment")
    prog_id = data.get("opc_prog_id")
    if not valid_text(prog_id, 256):
        raise ValueError("invalid_prog_id")
    interval_ms = data.get("interval_ms")
    if type(interval_ms) is not int or not 1000 <= interval_ms <= 60000:
        raise ValueError("invalid_interval")
    tags = data.get("tags")
    if not isinstance(tags, list) or not 1 <= len(tags) <= MAX_TAGS:
        raise ValueError("invalid_tags")
    parsed_tags = []
    seen_paths = set()
    for entry in tags:
        if isinstance(entry, str):
            if not valid_text(entry, 1024) or entry in seen_paths:
                raise ValueError("invalid_tags")
            seen_paths.add(entry)
            parsed_tags.append(entry)
        elif isinstance(entry, dict):
            path = entry.get("opc_item_path") or entry.get("path")
            if not valid_text(path, 1024) or path in seen_paths:
                raise ValueError("invalid_tags")
            seen_paths.add(path)
            parsed_tags.append(entry)
        else:
            raise ValueError("invalid_tags")

    def _tag_key(item: Any) -> str:
        return item if isinstance(item, str) else str(item.get("opc_item_path") or item.get("path") or "")

    agent_id = data.get("agent_id")
    if agent_id is not None and (not isinstance(agent_id, str) or not agent_id.strip()):
        agent_id = None
    return {
        "name": name.strip(),
        "equipment_id": eq_id.strip(),
        "opc_prog_id": prog_id.strip(),
        "interval_ms": interval_ms,
        "tags": sorted(parsed_tags, key=_tag_key),
        "agent_id": agent_id.strip() if agent_id else None,
    }


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
                "active-config", "opc-servers", "tag-validations", "tag-config-operations", "live-values"}:
            return None
        agent_id, action = parts
        expected = "GET" if action in ("active-config", "opc-servers", "live-values") else "POST"
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
        if action == "live-values":
            live_data = self._bridge_server.get_live_values(agent_id)
            return self._json_response(start_response, "200 OK", live_data)
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

    def equipment_route(self, method: str, path: str, query: str, environ: dict, start_response):
        if not path.startswith("/api/v1/equipments"):
            return None
        subpath = path[len("/api/v1/equipments"):]
        if subpath and not subpath.startswith("/"):
            return None
        parts = [p for p in subpath.split("/") if p]

        # /api/v1/equipments
        if len(parts) == 0:
            if method == "GET":
                if query:
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_query"})
                with self._database.session() as repo:
                    rows = repo.list_equipments()
                return self._json_response(start_response, "200 OK", {"equipments": rows})
            if method == "POST":
                try:
                    data = read_json(environ)
                    payload = equipment_payload(data)
                except ValueError as exc:
                    err = str(exc) if str(exc) in {"invalid_ip", "invalid_name", "invalid_json"} else "invalid_configuration"
                    return self._json_response(start_response, "400 Bad Request", {"error": err})
                except TypeError:
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_configuration"})

                with self._database.session() as repo:
                    if payload["agent_id"]:
                        agent = repo.get_agent(payload["agent_id"])
                        if agent is None:
                            return self._json_response(start_response, "404 Not Found", {"error": "agent_not_found"})
                    equipment_id = str(uuid.uuid4())
                    repo.add_equipment(equipment_id, payload["name"], payload["ip_address"], payload["agent_id"])
                    if payload["agent_id"]:
                        repo.add_audit_event(
                            payload["agent_id"],
                            str(uuid.uuid4()),
                            "equipment.created",
                            json.dumps({"equipment_id": equipment_id, "name": payload["name"], "ip": payload["ip_address"]}),
                        )
                    created = repo.get_equipment(equipment_id)
                return self._json_response(start_response, "201 Created", {"equipment": created})
            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

        # /api/v1/equipments/{equipment_id} or /api/v1/equipments/{equipment_id}/delete
        equipment_id = parts[0]
        if not equipment_id:
            return self._json_response(start_response, "400 Bad Request", {"error": "invalid_request"})

        is_delete = (len(parts) == 1 and method == "DELETE") or (
            len(parts) == 2 and parts[1] == "delete" and method in ("POST", "DELETE")
        )
        if is_delete:
            params = parse_qs(query) if query else {}
            confirmed = (
                params.get("confirmed", ["false"])[0].lower() in ("true", "1")
                or params.get("force", ["false"])[0].lower() in ("true", "1")
            )
            if not confirmed:
                try:
                    body_data = read_optional_json(environ)
                    if body_data.get("confirmed") is True or body_data.get("force") is True:
                        confirmed = True
                except Exception:
                    pass
            with self._database.session() as repo:
                existing = repo.get_equipment(equipment_id)
                if existing is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
                configs = repo.list_named_configs_for_equipment(equipment_id)
                if configs and not confirmed:
                    return self._json_response(
                        start_response,
                        "409 Conflict",
                        {
                            "error": "equipment_has_linked_configs",
                            "configs": [{"config_id": c["config_id"], "name": c["name"]} for c in configs],
                        },
                    )
                repo._execute("DELETE FROM named_opc_configs WHERE equipment_id = ?", (equipment_id,))
                repo.delete_equipment(equipment_id)
                if existing.get("agent_id"):
                    repo.add_audit_event(
                        existing["agent_id"],
                        str(uuid.uuid4()),
                        "equipment.deleted",
                        json.dumps({"equipment_id": equipment_id, "name": existing["name"]}),
                    )
            return self._json_response(start_response, "200 OK", {"deleted": True, "equipment_id": equipment_id})

        if len(parts) == 1:
            if method == "GET":
                with self._database.session() as repo:
                    row = repo.get_equipment(equipment_id)
                if row is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
                return self._json_response(start_response, "200 OK", {"equipment": row})
            if method in ("PUT", "POST"):
                try:
                    data = read_json(environ)
                    payload = equipment_payload(data)
                except ValueError as exc:
                    err = str(exc) if str(exc) in {"invalid_ip", "invalid_name", "invalid_json"} else "invalid_configuration"
                    return self._json_response(start_response, "400 Bad Request", {"error": err})
                except TypeError:
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_configuration"})

                with self._database.session() as repo:
                    existing = repo.get_equipment(equipment_id)
                    if existing is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
                    if payload["agent_id"]:
                        agent = repo.get_agent(payload["agent_id"])
                        if agent is None:
                            return self._json_response(start_response, "404 Not Found", {"error": "agent_not_found"})
                    repo.update_equipment(equipment_id, payload["name"], payload["ip_address"], payload["agent_id"])
                    if payload["agent_id"]:
                        repo.add_audit_event(
                            payload["agent_id"],
                            str(uuid.uuid4()),
                            "equipment.updated",
                            json.dumps({"equipment_id": equipment_id, "name": payload["name"], "ip": payload["ip_address"]}),
                        )
                    updated = repo.get_equipment(equipment_id)
                return self._json_response(start_response, "200 OK", {"equipment": updated})
            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})

    def named_config_route(self, method: str, path: str, query: str, environ: dict, start_response):
        prefix = None
        for p in ("/api/v1/opc-configs", "/api/v1/named-opc-configs"):
            if path == p or path.startswith(p + "/"):
                prefix = p
                break
        if prefix is None:
            return None
        subpath = path[len(prefix):]
        parts = [p for p in subpath.split("/") if p]

        # /api/v1/opc-configs
        if len(parts) == 0:
            if method == "GET":
                params = parse_qs(query) if query else {}
                equipment_id = params.get("equipment_id", [None])[0]
                with self._database.session() as repo:
                    rows = repo.list_named_configs(equipment_id)
                for r in rows:
                    r["tags"] = json.loads(r["tags_json"])
                return self._json_response(start_response, "200 OK", {"configs": rows})
            if method == "POST":
                try:
                    data = read_json(environ)
                    payload = named_config_payload(data)
                except ValueError as exc:
                    err = (
                        str(exc)
                        if str(exc)
                        in {
                            "invalid_name",
                            "invalid_equipment",
                            "invalid_prog_id",
                            "invalid_interval",
                            "invalid_tags",
                            "invalid_json",
                        }
                        else "invalid_configuration"
                    )
                    return self._json_response(start_response, "400 Bad Request", {"error": err})
                except TypeError:
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_configuration"})

                with self._database.session() as repo:
                    eq = repo.get_equipment(payload["equipment_id"])
                    if eq is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})
                    agent_id = payload.get("agent_id") or eq.get("agent_id")
                    config_id = str(uuid.uuid4())
                    repo.add_named_config(
                        config_id,
                        payload["name"],
                        payload["equipment_id"],
                        payload["opc_prog_id"],
                        payload["interval_ms"],
                        json.dumps(payload["tags"]),
                        agent_id,
                    )
                    if agent_id:
                        repo.add_audit_event(
                            agent_id,
                            str(uuid.uuid4()),
                            "opc_config.created",
                            json.dumps({
                                "config_id": config_id,
                                "name": payload["name"],
                                "equipment_id": payload["equipment_id"],
                                "tag_count": len(payload["tags"]),
                            }),
                        )
                    created = repo.get_named_config(config_id)
                    if created:
                        created["tags"] = json.loads(created["tags_json"])
                return self._json_response(start_response, "201 Created", {"config": created})
            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

        config_id = parts[0]
        if not config_id:
            return self._json_response(start_response, "400 Bad Request", {"error": "invalid_request"})

        is_delete = (len(parts) == 1 and method == "DELETE") or (
            len(parts) == 2 and parts[1] == "delete" and method in ("POST", "DELETE")
        )
        if is_delete:
            with self._database.session() as repo:
                existing = repo.get_named_config(config_id)
                if existing is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
                repo.delete_named_config(config_id)
                if existing.get("agent_id"):
                    repo.add_audit_event(
                        existing["agent_id"],
                        str(uuid.uuid4()),
                        "opc_config.deleted",
                        json.dumps({"config_id": config_id, "name": existing["name"]}),
                    )
            return self._json_response(start_response, "200 OK", {"deleted": True, "config_id": config_id})

        if len(parts) == 1:
            if method == "GET":
                with self._database.session() as repo:
                    row = repo.get_named_config(config_id)
                if row is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
                row["tags"] = json.loads(row["tags_json"])
                return self._json_response(start_response, "200 OK", {"config": row})
            if method in ("PUT", "POST"):
                try:
                    data = read_json(environ)
                    payload = named_config_payload(data)
                except ValueError as exc:
                    err = (
                        str(exc)
                        if str(exc)
                        in {
                            "invalid_name",
                            "invalid_equipment",
                            "invalid_prog_id",
                            "invalid_interval",
                            "invalid_tags",
                            "invalid_json",
                        }
                        else "invalid_configuration"
                    )
                    return self._json_response(start_response, "400 Bad Request", {"error": err})
                except TypeError:
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_configuration"})

                with self._database.session() as repo:
                    existing = repo.get_named_config(config_id)
                    if existing is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
                    eq = repo.get_equipment(payload["equipment_id"])
                    if eq is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})
                    agent_id = payload.get("agent_id") or eq.get("agent_id")
                    repo.update_named_config(
                        config_id,
                        payload["name"],
                        payload["equipment_id"],
                        payload["opc_prog_id"],
                        payload["interval_ms"],
                        json.dumps(payload["tags"]),
                        agent_id,
                    )
                    if agent_id:
                        repo.add_audit_event(
                            agent_id,
                            str(uuid.uuid4()),
                            "opc_config.updated",
                            json.dumps({
                                "config_id": config_id,
                                "name": payload["name"],
                                "tag_count": len(payload["tags"]),
                            }),
                        )
                    updated = repo.get_named_config(config_id)
                    if updated:
                        updated["tags"] = json.loads(updated["tags_json"])
                return self._json_response(start_response, "200 OK", {"config": updated})
            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
