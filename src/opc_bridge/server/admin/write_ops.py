"""Administrative endpoints for safe OPC write operations, per-tag allowlists, and persistent auditing."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

from opc_bridge.protocol.write import (
    WRITES_DISABLED_MESSAGE,
    WriteItemRequest,
    WriteRequest,
    is_writes_enabled,
    validate_item_value,
)
from opc_bridge.server.admin.tags import read_json

logger = logging.getLogger(__name__)


def parse_tag_specification(entry: Any) -> dict[str, Any] | None:
    """Parse tag entry, guaranteeing write_enabled defaults strictly to False."""
    if isinstance(entry, str):
        path = entry.strip()
        if not path:
            return None
        return {
            "opc_item_path": path,
            "description": path,
            "write_enabled": False,  # Strict default
            "data_type": "float",
            "min_value": None,
            "max_value": None,
            "allowed_values": None,
        }
    if isinstance(entry, dict):
        path = str(entry.get("opc_item_path") or entry.get("path") or "").strip()
        if not path:
            return None
        write_enabled = bool(entry.get("write_enabled", False))  # Strict default False
        data_type = str(entry.get("data_type", "float")).strip().lower()
        if data_type not in {"boolean", "integer", "float", "string"}:
            data_type = "float"
        desc = str(entry.get("description") or entry.get("name") or path).strip()
        return {
            "opc_item_path": path,
            "description": desc,
            "write_enabled": write_enabled,
            "data_type": data_type,
            "min_value": entry.get("min_value"),
            "max_value": entry.get("max_value"),
            "allowed_values": entry.get("allowed_values"),
        }
    return None


class WriteOperationAdministration:
    """Mixin for administrative write operation endpoints, validation, and auditing."""

    def _get_equipment_all_tags(self, equipment_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        with self._database.session() as repo:
            eq = repo.get_equipment(equipment_id)
            if eq is None:
                return None, [], ""
            configs = repo.list_named_configs(equipment_id)

        all_tags: list[dict[str, Any]] = []
        opc_prog_id = ""
        seen_paths: set[str] = set()

        for cfg in configs:
            if not opc_prog_id and cfg.get("opc_prog_id"):
                opc_prog_id = cfg["opc_prog_id"]
            raw_tags = []
            try:
                raw_tags = json.loads(cfg.get("tags_json", "[]"))
            except Exception:
                continue

            for raw in raw_tags:
                tag_spec = parse_tag_specification(raw)
                if tag_spec:
                    path = tag_spec["opc_item_path"]
                    if path not in seen_paths:
                        seen_paths.add(path)
                        tag_spec["prog_id"] = cfg.get("opc_prog_id", opc_prog_id)
                        all_tags.append(tag_spec)

        return eq, all_tags, opc_prog_id

    def _get_equipment_writable_tags(self, equipment_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        eq, all_tags, prog_id = self._get_equipment_all_tags(equipment_id)
        return eq, [t for t in all_tags if t.get("write_enabled")], prog_id

    def write_operation_route(self, method: str, path: str, query: str, environ: dict[str, Any], start_response):
        prefix = "/api/v1/write-operation/"
        if not path.startswith(prefix):
            return None

        action = path[len(prefix):].rstrip("/")

        # 1. GET /api/v1/write-operation/tags?equipment_id=<id>
        if action == "tags":
            if method != "GET":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
            params = parse_qs(query) if query else {}
            equipment_id = params.get("equipment_id", [None])[0]
            if not equipment_id:
                return self._json_response(start_response, "400 Bad Request", {"error": "equipment_id_required"})

            eq, tags, prog_id = self._get_equipment_all_tags(equipment_id)
            if eq is None:
                return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})

            agent_id = eq.get("agent_id")
            live_map: dict[str, dict[str, Any]] = {}
            if agent_id and self._bridge_server is not None:
                live_data = self._bridge_server.get_live_values(agent_id)
                for item in live_data.get("items", []):
                    live_map[item["opc_item_path"]] = item

            # Augment tags with current live reading if present
            augmented_tags: list[dict[str, Any]] = []
            for tag in tags:
                path_key = tag["opc_item_path"]
                live_item = live_map.get(path_key)
                tag_copy = dict(tag)
                if live_item:
                    tag_copy["current_value"] = live_item.get("value")
                    tag_copy["quality"] = live_item.get("quality")
                    tag_copy["quality_text"] = live_item.get("quality_text")
                    tag_copy["timestamp"] = live_item.get("opc_timestamp") or live_item.get("received_at")
                else:
                    tag_copy["current_value"] = None
                    tag_copy["quality"] = None
                    tag_copy["quality_text"] = None
                    tag_copy["timestamp"] = None
                augmented_tags.append(tag_copy)

            return self._json_response(
                start_response,
                "200 OK",
                {
                    "equipment": eq,
                    "writes_enabled_globally": is_writes_enabled(),
                    "opc_prog_id": prog_id,
                    "tags": augmented_tags,
                },
            )

        # 2. POST /api/v1/write-operation/validate
        if action == "validate":
            if method != "POST":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
            try:
                data = read_json(environ)
            except Exception:
                return self._json_response(start_response, "400 Bad Request", {"error": "invalid_json"})

            equipment_id = data.get("equipment_id")
            raw_items = data.get("items")
            if not equipment_id or not isinstance(raw_items, list):
                return self._json_response(start_response, "400 Bad Request", {"error": "invalid_parameters"})

            eq, tags, _ = self._get_equipment_all_tags(equipment_id)
            if eq is None:
                return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})

            tag_specs = {t["opc_item_path"]: t for t in tags}
            validation_results = []
            all_valid = True

            for raw_it in raw_items:
                if not isinstance(raw_it, dict):
                    continue
                tag_name = raw_it.get("tag")
                val = raw_it.get("value")
                spec = tag_specs.get(tag_name)
                if spec is None:
                    validation_results.append({
                        "tag": tag_name,
                        "valid": False,
                        "coerced_value": None,
                        "error": f"Tag '{tag_name}' is not configured for this equipment",
                    })
                    all_valid = False
                    continue

                if not spec.get("write_enabled", False):
                    validation_results.append({
                        "tag": tag_name,
                        "valid": False,
                        "coerced_value": None,
                        "error": f"Tag '{tag_name}' is not authorized for write operations (write_enabled=false)",
                    })
                    all_valid = False
                    continue

                valid, coerced, err = validate_item_value(
                    val,
                    spec["data_type"],
                    spec.get("min_value"),
                    spec.get("max_value"),
                    spec.get("allowed_values"),
                )
                if not valid:
                    all_valid = False
                validation_results.append({
                    "tag": tag_name,
                    "valid": valid,
                    "coerced_value": coerced,
                    "error": err,
                })

            return self._json_response(
                start_response,
                "200 OK",
                {"valid": all_valid, "items": validation_results},
            )

        # 3. POST /api/v1/write-operation/execute
        if action == "execute":
            if method != "POST":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

            # Global Kill Switch Check
            if not is_writes_enabled():
                return self._json_response(
                    start_response,
                    "403 Forbidden",
                    {"error": "writes_disabled", "message": WRITES_DISABLED_MESSAGE},
                )

            try:
                data = read_json(environ)
            except Exception:
                return self._json_response(start_response, "400 Bad Request", {"error": "invalid_json"})

            # Explicit Confirmation Check
            if data.get("confirmed") is not True:
                return self._json_response(
                    start_response,
                    "400 Bad Request",
                    {"error": "confirmation_required", "message": "Explicit confirmation required to write"},
                )

            equipment_id = data.get("equipment_id")
            raw_items = data.get("items")
            if not equipment_id or not isinstance(raw_items, list) or len(raw_items) == 0:
                return self._json_response(start_response, "400 Bad Request", {"error": "invalid_parameters"})

            eq, tags, prog_id = self._get_equipment_all_tags(equipment_id)
            if eq is None:
                return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})

            agent_id = eq.get("agent_id")
            if not agent_id:
                return self._json_response(
                    start_response,
                    "409 Conflict",
                    {"error": "equipment_has_no_agent", "message": "Equipment has no associated agent"},
                )

            if self._bridge_server is None:
                return self._json_response(
                    start_response,
                    "503 Service Unavailable",
                    {"error": "runtime_unavailable", "message": "Bridge server is not running"},
                )

            session = next((s for s in self._bridge_server._sessions.values() if s.agent_id == agent_id), None)
            if session is None:
                return self._json_response(
                    start_response,
                    "409 Conflict",
                    {"error": "agent_disconnected", "message": "Agent is disconnected"},
                )

            tag_specs = {t["opc_item_path"]: t for t in tags}
            write_items: list[WriteItemRequest] = []
            previous_values: dict[str, Any] = {}

            # Cache current live values for previous_value audit
            live_data = self._bridge_server.get_live_values(agent_id)
            live_map = {it["opc_item_path"]: it.get("value") for it in live_data.get("items", [])}

            for raw_it in raw_items:
                if not isinstance(raw_it, dict):
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_item_format"})
                tag_name = raw_it.get("tag")
                val = raw_it.get("value")

                # Double check per-tag write authorization
                spec = tag_specs.get(tag_name)
                if spec is None or not spec["write_enabled"]:
                    return self._json_response(
                        start_response,
                        "403 Forbidden",
                        {
                            "error": "unauthorized_tag",
                            "message": f"Tag '{tag_name}' is not authorized for write operations",
                        },
                    )

                valid, coerced, err = validate_item_value(
                    val,
                    spec["data_type"],
                    spec.get("min_value"),
                    spec.get("max_value"),
                    spec.get("allowed_values"),
                )
                if not valid:
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {"error": "validation_failed", "message": f"Validation failed for '{tag_name}': {err}"},
                    )

                write_items.append(WriteItemRequest(tag=tag_name, value=coerced, data_type=spec["data_type"]))
                previous_values[tag_name] = live_map.get(tag_name)

            user = str(data.get("username") or data.get("user") or environ.get("REMOTE_USER") or "admin").strip() or "admin"
            op_prog_id = prog_id or session.opc_prog_id or "OPC.Server"
            operation_id = str(uuid.uuid4())
            write_req = WriteRequest(
                request_id=operation_id,
                opc_prog_id=op_prog_id,
                items=write_items,
                user=user,
            )

            try:
                write_resp = self._bridge_server.write_agent_threadsafe(agent_id, write_req)
            except Exception as exc:
                err_str = str(exc)
                logger.exception("Error executing write on agent %s: %s", agent_id, exc)
                return self._json_response(
                    start_response,
                    "504 Gateway Timeout" if "timeout" in err_str else "409 Conflict",
                    {"error": "write_failed", "message": err_str},
                )

            # Record persistent audit trail for each tag
            with self._database.session() as repo:
                for res in (write_resp.results or []):
                    audit_detail = {
                        "operation_id": operation_id,
                        "equipment_id": equipment_id,
                        "user": user,
                        "tag": res.tag,
                        "previous_value": previous_values.get(res.tag),
                        "requested_value": res.value,
                        "status": res.status,
                        "error": res.error,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    repo.add_audit_event(
                        agent_id,
                        str(uuid.uuid4()),
                        "opc_write.executed",
                        json.dumps(audit_detail),
                    )

            return self._json_response(
                start_response,
                "200 OK",
                {
                    "operation_id": operation_id,
                    "equipment_id": equipment_id,
                    "status": "completed",
                    "results": [r.to_dict() for r in (write_resp.results or [])],
                },
            )

        # 4. GET /api/v1/write-operation/audit?equipment_id=<id>&limit=<n>
        if action == "audit":
            if method != "GET":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
            params = parse_qs(query) if query else {}
            equipment_id = params.get("equipment_id", [None])[0]
            limit_str = params.get("limit", ["50"])[0]
            try:
                limit = int(limit_str)
            except ValueError:
                limit = 50

            agent_id = None
            if equipment_id:
                with self._database.session() as repo:
                    eq = repo.get_equipment(equipment_id)
                    if eq:
                        agent_id = eq.get("agent_id")

            with self._database.session() as repo:
                query_sql = (
                    "SELECT event_id, agent_id, event_type, detail_json, occurred_at "
                    "FROM audit_events WHERE event_type LIKE 'opc_write%' "
                )
                sql_params: tuple = ()
                if agent_id:
                    query_sql += "AND agent_id = ? "
                    sql_params = (agent_id,)
                query_sql += f"ORDER BY occurred_at DESC LIMIT {max(1, min(limit, 200))}"
                rows = repo._dicts(repo._execute(query_sql, sql_params))

            events = []
            for r in rows:
                try:
                    detail = json.loads(r.get("detail_json", "{}"))
                except Exception:
                    detail = {}
                events.append({
                    "event_id": r["event_id"],
                    "agent_id": r["agent_id"],
                    "occurred_at": r["occurred_at"],
                    "detail": detail,
                })

            return self._json_response(start_response, "200 OK", {"audit_events": events})

        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
