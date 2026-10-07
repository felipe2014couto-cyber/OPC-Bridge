"""Administrative API endpoints for OPC-to-PI mappings, validation, controlled simulation, and persistent auditing."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional
from urllib.parse import parse_qs

from opc_bridge.server.admin.tags import read_json
from opc_bridge.server.pi_output import (
    PiOutputChannel,
    PiOutputConfig,
    SimulatedPiOutputChannel,
    create_pi_output_channel,
    evaluate_mapping_publication,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)


def validate_pi_mapping_input(data: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Validate mapping payload and ensure publish_interval_ms >= config.interval_ms."""
    if not isinstance(data, dict):
        raise ValueError("invalid_configuration")

    point_name = str(data.get("pi_point_name") or "").strip()
    if not point_name or len(point_name) > 255:
        raise ValueError("invalid_pi_point_name")

    point_source = str(data.get("point_source") or "").strip()
    if len(point_source) > 64:
        raise ValueError("invalid_point_source")

    raw_loc1 = data.get("location1", 0)
    try:
        location1 = int(raw_loc1)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_location1") from exc

    raw_interval = data.get("publish_interval_ms")
    if type(raw_interval) is not int:
        try:
            raw_interval = int(raw_interval)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_publish_interval") from exc

    if not 1000 <= raw_interval <= 60000:
        raise ValueError("invalid_publish_interval_bounds")

    # Critical industrial safety rule: publication rate cannot be faster than OPC update rate
    opc_rate = config.get("interval_ms") or config.get("opc_interval_ms") or 1000
    if raw_interval < opc_rate:
        raise ValueError("interval_faster_than_opc")

    raw_enabled = data.get("enabled", True)
    enabled = bool(raw_enabled) if raw_enabled is not None else True

    return {
        "pi_point_name": point_name,
        "point_source": point_source,
        "location1": location1,
        "publish_interval_ms": raw_interval,
        "enabled": enabled,
    }


class PiIntegrationAdministration:
    """Mixin for managing PI Point mappings and executing simulated or real PI output."""

    def _get_pi_output_config(self) -> PiOutputConfig:
        if getattr(self, "_pi_output_config", None) is not None:
            return self._pi_output_config
        return PiOutputConfig.load_from_env()

    def _get_pi_output_channel(self) -> PiOutputChannel:
        if getattr(self, "_pi_output_channel", None) is not None:
            return self._pi_output_channel
        pub = getattr(self._bridge_server, "_pi_publisher", None) if getattr(self, "_bridge_server", None) else None
        if pub is not None and getattr(pub, "channel", None) is not None:
            return pub.channel
        cfg = self._get_pi_output_config()
        return create_pi_output_channel(cfg)

    def pi_integration_route(
        self, method: str, path: str, query: str, environ: dict[str, Any], start_response
    ):
        prefix = "/api/v1/pi-integration"
        if path != prefix and not path.startswith(prefix + "/"):
            return None

        subpath = path[len(prefix):]
        parts = [p for p in subpath.split("/") if p]

        # 1. GET /api/v1/pi-integration/status
        if len(parts) == 1 and parts[0] == "status":
            if method != "GET":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
            cfg = self._get_pi_output_config()
            banner_text = "Saída PI habilitada" if cfg.enabled else "Saída PI: Simulação — nenhuma escrita real habilitada."
            return self._json_response(
                start_response,
                "200 OK",
                {
                    "output_enabled": cfg.enabled,
                    "output_mode": cfg.mode,
                    "banner_text": banner_text,
                },
            )

        # 2. POST /api/v1/pi-integration/test-connection
        if len(parts) == 1 and parts[0] == "test-connection":
            if method != "POST":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
            channel = self._get_pi_output_channel()
            test_result = channel.test_connection()
            return self._json_response(
                start_response,
                "200 OK",
                test_result,
            )

        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})

    def pi_mapping_route(
        self, method: str, path: str, query: str, environ: dict[str, Any], start_response
    ):
        if path.startswith("/api/v1/pi-integration"):
            return self.pi_integration_route(method, path, query, environ, start_response)

        prefix = "/api/v1/pi-mappings"
        if path != prefix and not path.startswith(prefix + "/"):
            return None

        subpath = path[len(prefix):]
        parts = [p for p in subpath.split("/") if p]
        user = str(environ.get("REMOTE_USER") or "admin").strip() or "admin"

        # 1. GET or POST /api/v1/pi-mappings
        if len(parts) == 0:
            if method == "GET":
                params = parse_qs(query) if query else {}
                equipment_id = params.get("equipment_id", [None])[0]
                opc_config_id = params.get("opc_config_id", [None])[0]

                with self._database.session() as repo:
                    mappings = repo.list_pi_mappings(equipment_id, opc_config_id)

                # Augment mappings with live readings already captured from OPC
                live_map: dict[str, dict[str, Any]] = {}
                if self._bridge_server is not None:
                    # Gather live readings for relevant agents
                    seen_agents: set[str] = {m["agent_id"] for m in mappings if m.get("agent_id")}
                    for ag_id in seen_agents:
                        live_data = self._bridge_server.get_live_values(ag_id)
                        for it in live_data.get("items", []):
                            live_map[(ag_id, it["opc_item_path"])] = it

                augmented = []
                for m in mappings:
                    m_copy = dict(m)
                    ag_id = m.get("agent_id")
                    tag_path = m.get("opc_item_path")
                    live_it = live_map.get((ag_id, tag_path)) if ag_id and tag_path else None
                    if live_it:
                        m_copy["current_value"] = live_it.get("value")
                        m_copy["quality"] = live_it.get("quality")
                        m_copy["quality_text"] = live_it.get("quality_text")
                        m_copy["opc_timestamp"] = live_it.get("opc_timestamp") or live_it.get("received_at")
                        m_copy["age_ms"] = live_it.get("age_ms")
                        m_copy["stale"] = bool(live_it.get("stale") or live_it.get("status") == "stale")
                    else:
                        m_copy["current_value"] = None
                        m_copy["quality"] = None
                        m_copy["quality_text"] = None
                        m_copy["opc_timestamp"] = None
                        m_copy["age_ms"] = None
                        m_copy["stale"] = False
                    augmented.append(m_copy)

                return self._json_response(start_response, "200 OK", {"mappings": augmented})

            if method == "POST":
                try:
                    data = read_json(environ)
                except Exception:
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_json"})

                equipment_id = str(data.get("equipment_id") or "").strip()
                opc_config_id = str(data.get("opc_config_id") or "").strip()
                opc_item_path = str(data.get("opc_item_path") or "").strip()
                raw_item_id = data.get("item_id", 0)
                try:
                    item_id = int(raw_item_id)
                except (TypeError, ValueError):
                    item_id = 0

                if not equipment_id or not opc_config_id or not opc_item_path:
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {"error": "missing_required_fields", "message": "Equipamento, Configuração OPC e Tag são obrigatórios."},
                    )

                with self._database.session() as repo:
                    eq = repo.get_equipment(equipment_id)
                    if eq is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})

                    cfg = repo.get_named_config(opc_config_id)
                    if cfg is None or cfg.get("equipment_id") != equipment_id:
                        return self._json_response(start_response, "404 Not Found", {"error": "opc_config_not_found"})

                    tags_list = json.loads(cfg.get("tags_json", "[]"))
                    # Support both list of strings or list of dicts
                    known_paths = [t if isinstance(t, str) else (t.get("opc_item_path") or t.get("path")) for t in tags_list]
                    if opc_item_path not in known_paths:
                        return self._json_response(
                            start_response,
                            "400 Bad Request",
                            {"error": "tag_not_in_config", "message": f"A tag '{opc_item_path}' não pertence à configuração OPC selecionada."},
                        )

                    try:
                        validated = validate_pi_mapping_input(data, cfg)
                    except ValueError as exc:
                        code = str(exc)
                        if code == "interval_faster_than_opc":
                            return self._json_response(
                                start_response,
                                "400 Bad Request",
                                {
                                    "error": "interval_faster_than_opc",
                                    "message": (
                                        f"A velocidade de publicação informada ({data.get('publish_interval_ms')} ms) "
                                        f"não pode ser menor que o intervalo de coleta OPC ({cfg['interval_ms']} ms), "
                                        "pois não existem valores novos nessa frequência."
                                    ),
                                },
                            )
                        return self._json_response(start_response, "400 Bad Request", {"error": code})

                    # Check for duplicate mapping
                    existing_list = repo.list_pi_mappings(equipment_id=equipment_id, opc_config_id=opc_config_id)
                    for em in existing_list:
                        if em.get("opc_item_path") == opc_item_path:
                            return self._json_response(
                                start_response,
                                "409 Conflict",
                                {"error": "mapping_conflict", "message": f"Já existe um mapeamento cadastrado para a tag '{opc_item_path}' nesta configuração."},
                            )

                    mapping_id = str(uuid.uuid4())
                    repo.add_pi_mapping(
                        mapping_id=mapping_id,
                        equipment_id=equipment_id,
                        opc_config_id=opc_config_id,
                        opc_item_path=opc_item_path,
                        item_id=item_id,
                        pi_point_name=validated["pi_point_name"],
                        point_source=validated["point_source"],
                        location1=validated["location1"],
                        publish_interval_ms=validated["publish_interval_ms"],
                        enabled=validated["enabled"],
                    )

                    agent_id = cfg.get("agent_id") or eq.get("agent_id")
                    if agent_id:
                        repo.add_audit_event(
                            agent_id,
                            str(uuid.uuid4()),
                            "pi_mapping.created",
                            json.dumps({
                                "mapping_id": mapping_id,
                                "equipment_id": equipment_id,
                                "opc_config_id": opc_config_id,
                                "opc_item_path": opc_item_path,
                                "pi_point_name": validated["pi_point_name"],
                                "publish_interval_ms": validated["publish_interval_ms"],
                                "user": user,
                            }),
                        )

                    created = repo.get_pi_mapping(mapping_id)

                return self._json_response(start_response, "201 Created", {"mapping": created})

            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

        # 2. GET /api/v1/pi-mappings/audit
        if len(parts) == 1 and parts[0] == "audit":
            if method != "GET":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

            params = parse_qs(query) if query else {}
            equipment_id = params.get("equipment_id", [None])[0]
            limit_str = params.get("limit", ["50"])[0]
            try:
                limit = int(limit_str)
            except ValueError:
                limit = 50

            with self._database.session() as repo:
                agent_id = None
                if equipment_id:
                    eq = repo.get_equipment(equipment_id)
                    if eq:
                        agent_id = eq.get("agent_id")

                query_sql = (
                    "SELECT event_id, agent_id, event_type, detail_json, occurred_at "
                    "FROM audit_events WHERE event_type LIKE 'pi_mapping.%' "
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
                    det = json.loads(r.get("detail_json", "{}"))
                except Exception:
                    det = {}
                events.append({
                    "event_id": r["event_id"],
                    "agent_id": r["agent_id"],
                    "event_type": r["event_type"],
                    "occurred_at": r["occurred_at"],
                    "detail": det,
                })

            return self._json_response(start_response, "200 OK", {"audit_events": events})

        # 3. Actions on a specific mapping: /api/v1/pi-mappings/<id>[/action]
        mapping_id = parts[0]

        # Simulation: POST /api/v1/pi-mappings/<id>/simulate
        if len(parts) == 2 and parts[1] == "simulate":
            if method != "POST":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

            with self._database.session() as repo:
                mapping = repo.get_pi_mapping(mapping_id)
                if mapping is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})

                # Requirement 4: only active (enabled) mappings can be published/simulated
                if not mapping.get("enabled"):
                    return self._json_response(
                        start_response,
                        "409 Conflict",
                        {"error": "mapping_disabled", "message": "O mapeamento está desativado e não pode ser publicado no PI."},
                    )

                agent_id = mapping.get("agent_id")
                opc_path = mapping.get("opc_item_path")

                # Retrieve last reading already available from active cache (strictly read-only)
                live_item = None
                if agent_id and self._bridge_server is not None:
                    live_data = self._bridge_server.get_live_values(agent_id)
                    for it in live_data.get("items", []):
                        if it.get("opc_item_path") == opc_path:
                            live_item = it
                            break

                current_val = live_item.get("value") if live_item else None
                opc_ts = live_item.get("opc_timestamp") if live_item else None
                quality = live_item.get("quality") if live_item else None

                # Execute controlled simulated publication
                channel = self._get_pi_output_channel()
                sim_res = channel.publish(
                    pi_point_name=mapping["pi_point_name"],
                    value=current_val if current_val is not None else 0.0,
                    timestamp=opc_ts,
                    quality=quality,
                    point_source=mapping.get("point_source", ""),
                    location1=mapping.get("location1", 0),
                )

                # Persist updated status
                val_str = str(current_val) if current_val is not None else "0.0"
                repo.update_pi_mapping_status(mapping_id, sim_res.status, val_str)

                # Record persistent audit event
                if agent_id:
                    repo.add_audit_event(
                        agent_id,
                        str(uuid.uuid4()),
                        "pi_mapping.simulation",
                        json.dumps({
                            "mapping_id": mapping_id,
                            "opc_item_path": opc_path,
                            "pi_point_name": mapping["pi_point_name"],
                            "point_source": mapping.get("point_source", ""),
                            "location1": mapping.get("location1", 0),
                            "value": current_val,
                            "quality": quality,
                            "opc_timestamp": opc_ts,
                            "status": sim_res.status,
                            "user": user,
                        }),
                    )

                updated = repo.get_pi_mapping(mapping_id)

            return self._json_response(
                start_response,
                "200 OK",
                {"result": sim_res.to_dict(), "mapping": updated},
            )

        # Single real/simulated publication: POST /api/v1/pi-mappings/<id>/publish-once
        if len(parts) == 2 and parts[1] == "publish-once":
            if method != "POST":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

            with self._database.session() as repo:
                mapping = repo.get_pi_mapping(mapping_id)
                if mapping is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})

                if not mapping.get("enabled"):
                    return self._json_response(
                        start_response,
                        "409 Conflict",
                        {"error": "mapping_disabled", "message": "O mapeamento está desativado e não pode ser publicado no PI."},
                    )

                agent_id = mapping.get("agent_id")
                opc_path = mapping.get("opc_item_path")

                # Retrieve last reading already available from active cache (strictly read-only)
                live_item = None
                if agent_id and self._bridge_server is not None:
                    live_data = self._bridge_server.get_live_values(agent_id)
                    for it in live_data.get("items", []):
                        if it.get("opc_item_path") == opc_path:
                            live_item = it
                            break

                channel = self._get_pi_output_channel()
                config = self._get_pi_output_config()

                eval_res = evaluate_mapping_publication(
                    mapping=mapping,
                    live_item=live_item,
                    channel=channel,
                    config=config,
                    force=True,
                )

                if eval_res.get("action") == "skipped":
                    reason = eval_res.get("reason")
                    msg = eval_res.get("message", "Publicação bloqueada.")
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {"error": reason, "message": msg},
                    )

                res = eval_res["result"]
                repo.update_pi_mapping_publication(
                    mapping_id=mapping_id,
                    status=eval_res["new_status"],
                    published_value=str(eval_res["value"]),
                    next_publish_due_at=eval_res.get("next_publish_due_at"),
                    error=eval_res.get("error"),
                    failure_count=eval_res.get("failure_count"),
                )

                if agent_id:
                    repo.add_audit_event(
                        agent_id,
                        str(uuid.uuid4()),
                        "pi_mapping.publish_once",
                        json.dumps({
                            "mapping_id": mapping_id,
                            "opc_item_path": opc_path,
                            "pi_point_name": mapping["pi_point_name"],
                            "point_source": mapping.get("point_source", ""),
                            "location1": mapping.get("location1", 0),
                            "value": eval_res["value"],
                            "quality": eval_res["quality"],
                            "opc_timestamp": eval_res["timestamp"],
                            "status": eval_res["new_status"],
                            "mode": config.mode,
                            "error": eval_res.get("error"),
                            "user": user,
                        }),
                    )

                updated = repo.get_pi_mapping(mapping_id)

            return self._json_response(
                start_response,
                "200 OK",
                {"result": res.to_dict(), "mapping": updated},
            )

        # Toggle enabled: POST or PATCH /api/v1/pi-mappings/<id>/toggle
        if len(parts) == 2 and parts[1] == "toggle":
            if method not in ("POST", "PATCH", "PUT"):
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

            with self._database.session() as repo:
                mapping = repo.get_pi_mapping(mapping_id)
                if mapping is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})

                new_enabled = not bool(mapping.get("enabled"))
                repo.set_pi_mapping_enabled(mapping_id, new_enabled)

                agent_id = mapping.get("agent_id")
                if agent_id:
                    repo.add_audit_event(
                        agent_id,
                        str(uuid.uuid4()),
                        "pi_mapping.toggled",
                        json.dumps({
                            "mapping_id": mapping_id,
                            "pi_point_name": mapping["pi_point_name"],
                            "enabled": new_enabled,
                            "user": user,
                        }),
                    )

                updated = repo.get_pi_mapping(mapping_id)

            return self._json_response(start_response, "200 OK", {"mapping": updated})

        # Single mapping operations: GET, PUT, DELETE /api/v1/pi-mappings/<id>
        if len(parts) == 1:
            if method == "GET":
                with self._database.session() as repo:
                    mapping = repo.get_pi_mapping(mapping_id)
                if mapping is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})
                return self._json_response(start_response, "200 OK", {"mapping": mapping})

            if method in ("PUT", "POST"):
                try:
                    data = read_json(environ)
                except Exception:
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_json"})

                with self._database.session() as repo:
                    existing = repo.get_pi_mapping(mapping_id)
                    if existing is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})

                    cfg = repo.get_named_config(existing["opc_config_id"])
                    if cfg is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "opc_config_not_found"})

                    try:
                        validated = validate_pi_mapping_input(data, cfg)
                    except ValueError as exc:
                        code = str(exc)
                        if code == "interval_faster_than_opc":
                            return self._json_response(
                                start_response,
                                "400 Bad Request",
                                {
                                    "error": "interval_faster_than_opc",
                                    "message": (
                                        f"A velocidade de publicação informada ({data.get('publish_interval_ms')} ms) "
                                        f"não pode ser menor que o intervalo de coleta OPC ({cfg['interval_ms']} ms), "
                                        "pois não existem valores novos nessa frequência."
                                    ),
                                },
                            )
                        return self._json_response(start_response, "400 Bad Request", {"error": code})

                    repo.update_pi_mapping(
                        mapping_id=mapping_id,
                        pi_point_name=validated["pi_point_name"],
                        point_source=validated["point_source"],
                        location1=validated["location1"],
                        publish_interval_ms=validated["publish_interval_ms"],
                        enabled=validated["enabled"],
                    )

                    agent_id = existing.get("agent_id")
                    if agent_id:
                        repo.add_audit_event(
                            agent_id,
                            str(uuid.uuid4()),
                            "pi_mapping.updated",
                            json.dumps({
                                "mapping_id": mapping_id,
                                "pi_point_name": validated["pi_point_name"],
                                "publish_interval_ms": validated["publish_interval_ms"],
                                "enabled": validated["enabled"],
                                "user": user,
                            }),
                        )

                    updated = repo.get_pi_mapping(mapping_id)

                return self._json_response(start_response, "200 OK", {"mapping": updated})

            if method == "DELETE":
                with self._database.session() as repo:
                    existing = repo.get_pi_mapping(mapping_id)
                    if existing is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})

                    repo.delete_pi_mapping(mapping_id)

                    agent_id = existing.get("agent_id")
                    if agent_id:
                        repo.add_audit_event(
                            agent_id,
                            str(uuid.uuid4()),
                            "pi_mapping.deleted",
                            json.dumps({
                                "mapping_id": mapping_id,
                                "pi_point_name": existing["pi_point_name"],
                                "user": user,
                            }),
                        )

                return self._json_response(start_response, "200 OK", {"deleted": True, "mapping_id": mapping_id})

            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

        # Delete alias: POST /api/v1/pi-mappings/<id>/delete
        if len(parts) == 2 and parts[1] == "delete" and method in ("POST", "DELETE"):
            with self._database.session() as repo:
                existing = repo.get_pi_mapping(mapping_id)
                if existing is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})

                repo.delete_pi_mapping(mapping_id)

                agent_id = existing.get("agent_id")
                if agent_id:
                    repo.add_audit_event(
                        agent_id,
                        str(uuid.uuid4()),
                        "pi_mapping.deleted",
                        json.dumps({
                            "mapping_id": mapping_id,
                            "pi_point_name": existing["pi_point_name"],
                            "user": user,
                        }),
                    )

            return self._json_response(start_response, "200 OK", {"deleted": True, "mapping_id": mapping_id})

        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})
