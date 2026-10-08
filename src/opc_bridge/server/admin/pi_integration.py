"""Administrative API endpoints for OPC-to-PI mappings, validation, controlled simulation, and persistent auditing."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, List, Optional
from urllib.parse import parse_qs

from opc_bridge.protocol.inspection import InspectionRequest, InspectionResponse
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


def validate_pi_mapping_input(
    data: dict[str, Any],
    profile: Optional[dict[str, Any]] = None,
    config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Validate mapping payload and ensure publish_interval_ms is between 1000 and 60000 ms.

    PointSource and Location1 are inherited from the mandatory PI profile.
    """
    if not isinstance(data, dict):
        raise ValueError("invalid_configuration")

    point_name = str(data.get("pi_point_name") or "").strip()
    if not point_name or len(point_name) > 255:
        raise ValueError("invalid_pi_point_name")

    if profile is not None:
        point_source = str(profile.get("point_source") or "").strip()
        location1 = int(profile.get("location1", 0))
    else:
        point_source = str(data.get("point_source") or "").strip()
        raw_loc1 = data.get("location1", 0)
        try:
            location1 = int(raw_loc1)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_location1") from exc

    if not point_source or len(point_source) > 64:
        raise ValueError("invalid_point_source")

    raw_interval = data.get("publish_interval_ms")
    if type(raw_interval) is not int:
        try:
            raw_interval = int(raw_interval)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_publish_interval") from exc

    if not 1000 <= raw_interval <= 60000:
        raise ValueError("invalid_publish_interval_bounds")

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
        pub = (
            getattr(self._bridge_server, "_pi_publisher", None)
            if getattr(self, "_bridge_server", None)
            else None
        )
        if pub is not None and getattr(pub, "channel", None) is not None:
            return pub.channel
        cfg = self._get_pi_output_config()
        return create_pi_output_channel(cfg)

    def _get_pi_live_cache(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        if not hasattr(self, "_pi_live_values"):
            self._pi_live_values = {}
        return self._pi_live_values

    def _get_live_item(
        self, equipment_id: str, opc_prog_id: str, opc_item_path: str, agent_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        cache = self._get_pi_live_cache()
        live_it = cache.get((equipment_id, opc_prog_id, opc_item_path))
        if live_it:
            return live_it
        if not agent_id and equipment_id:
            with self._database.session() as repo:
                eq = repo.get_equipment(equipment_id)
                if eq:
                    agent_id = eq.get("agent_id")
        if agent_id and self._bridge_server:
            live_data = self._bridge_server.get_live_values(agent_id) or {}
            for it in live_data.get("items", []):
                if it.get("opc_item_path") == opc_item_path:
                    return it
        return None

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
            banner_text = "Saída PI habilitada" if cfg.enabled else "Saída PI: desabilitada — nenhuma escrita real habilitada."
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
            cfg = self._get_pi_output_config()
            if not cfg.enabled and cfg.mode == "web_api":
                return self._json_response(
                    start_response,
                    "200 OK",
                    {
                        "connected": False,
                        "output_enabled": False,
                        "error": "output_disabled",
                        "message": "Saída PI: desabilitada — nenhuma escrita real habilitada. Teste de conexão bloqueado.",
                    },
                )
            channel = self._get_pi_output_channel()
            test_result = channel.test_connection()
            return self._json_response(
                start_response,
                "200 OK",
                test_result,
            )

        # 3. GET /api/v1/pi-integration/discover-servers?equipment_id=<id>
        if len(parts) == 1 and parts[0] == "discover-servers":
            if method != "GET":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
            params = parse_qs(query) if query else {}
            equipment_id = params.get("equipment_id", [None])[0]
            if not equipment_id:
                return self._json_response(
                    start_response, "400 Bad Request", {"error": "missing_equipment_id", "message": "Equipamento obrigatório."}
                )
            with self._database.session() as repo:
                eq = repo.get_equipment(equipment_id)
            if eq is None:
                return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})

            agent_id = eq.get("agent_id")
            if not agent_id or self._bridge_server is None:
                return self._json_response(
                    start_response,
                    "200 OK",
                    {"servers": [], "message": "Nenhum agente vinculado ao equipamento selecionado."},
                )

            try:
                req = InspectionRequest(str(uuid.uuid4()), action="servers")
                resp = self._bridge_server.inspect_agent_threadsafe(agent_id, req)
                servers = resp.servers or []
                return self._json_response(start_response, "200 OK", {"servers": servers})
            except Exception as exc:
                sanitized = sanitize_error_message(exc)
                logger.warning("Falha na descoberta de servidores OPC para equipamento %s: %s", equipment_id, sanitized)
                return self._json_response(
                    start_response, "200 OK", {"servers": [], "warning": f"Não foi possível listar servidores: {sanitized}."}
                )

        # 4. POST /api/v1/pi-integration/read-now
        if len(parts) == 1 and parts[0] == "read-now":
            if method != "POST":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
            try:
                data = read_json(environ)
            except Exception:
                return self._json_response(start_response, "400 Bad Request", {"error": "invalid_json"})

            equipment_id = str(data.get("equipment_id") or "").strip()
            opc_prog_id = str(data.get("opc_prog_id") or "").strip()
            raw_tags = data.get("tags") or []
            tags = [str(t).strip() for t in raw_tags if str(t).strip()]

            if not equipment_id or not opc_prog_id or not tags:
                return self._json_response(
                    start_response,
                    "400 Bad Request",
                    {
                        "error": "missing_required_fields",
                        "message": "Equipamento, Servidor OPC e lista de tags são obrigatórios para leitura.",
                    },
                )

            with self._database.session() as repo:
                eq = repo.get_equipment(equipment_id)
            if eq is None:
                return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})

            agent_id = eq.get("agent_id")
            if not agent_id or self._bridge_server is None:
                return self._json_response(
                    start_response,
                    "409 Conflict",
                    {
                        "error": "agent_unavailable",
                        "message": "Nenhum agente online conectado ao equipamento para leitura OPC.",
                    },
                )

            # Chunk tags into batches of up to 50
            all_results: List[dict[str, Any]] = []
            chunk_size = 50
            for i in range(0, len(tags), chunk_size):
                chunk = tags[i:i + chunk_size]
                try:
                    req = InspectionRequest(str(uuid.uuid4()), action="tags", opc_prog_id=opc_prog_id, tags=chunk)
                    resp = self._bridge_server.inspect_agent_threadsafe(agent_id, req)
                    chunk_results = resp.results or []
                    all_results.extend(chunk_results)
                except Exception as exc:
                    err_msg = sanitize_error_message(exc)
                    logger.warning("Falha na leitura OPC isolada para tags %s: %s", chunk, err_msg)
                    for tag in chunk:
                        all_results.append({
                            "opc_item_path": tag,
                            "status": "error",
                            "value": None,
                            "quality": None,
                            "quality_text": None,
                            "opc_timestamp": None,
                            "error": err_msg,
                        })

            # Cache results in memory and update DB mappings if present
            cache = self._get_pi_live_cache()
            with self._database.session() as repo:
                existing_maps = repo.list_pi_mappings(equipment_id=equipment_id, opc_prog_id=opc_prog_id)
                tag_to_map_id = {m["opc_item_path"]: m["mapping_id"] for m in existing_maps}

                for r in all_results:
                    tag = r.get("opc_item_path", "")
                    cache[(equipment_id, opc_prog_id, tag)] = r
                    m_id = tag_to_map_id.get(tag)
                    if m_id and r.get("value") is not None:
                        repo.update_pi_mapping_status(m_id, "Lido via OPC", str(r.get("value")))

            return self._json_response(start_response, "200 OK", {"results": all_results})

        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})

    def pi_profile_route(
        self, method: str, path: str, query: str, environ: dict[str, Any], start_response
    ):
        prefix = "/api/v1/pi-profiles"
        if path != prefix and not path.startswith(prefix + "/"):
            return None

        subpath = path[len(prefix):]
        parts = [p for p in subpath.split("/") if p]
        user = str(environ.get("REMOTE_USER") or "admin").strip() or "admin"

        # 1. GET or POST /api/v1/pi-profiles
        if len(parts) == 0:
            if method == "GET":
                params = parse_qs(query) if query else {}
                equipment_id = params.get("equipment_id", [None])[0]
                opc_prog_id = params.get("opc_prog_id", [None])[0]
                with self._database.session() as repo:
                    if equipment_id and opc_prog_id:
                        prof = repo.get_pi_profile(equipment_id, opc_prog_id)
                        return self._json_response(start_response, "200 OK", {"profile": prof})
                    profs = repo.list_pi_profiles(equipment_id)
                return self._json_response(start_response, "200 OK", {"profiles": profs})

            if method == "POST":
                try:
                    data = read_json(environ)
                except Exception:
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_json"})

                equipment_id = str(data.get("equipment_id") or "").strip()
                opc_prog_id = str(data.get("opc_prog_id") or "").strip()
                point_source = str(data.get("point_source") or "").strip()
                raw_loc1 = data.get("location1", 0)
                try:
                    location1 = int(raw_loc1)
                except (TypeError, ValueError):
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_location1"})

                if not equipment_id or not opc_prog_id:
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {"error": "missing_required_fields", "message": "Equipamento e Servidor OPC (ProgID) são obrigatórios."},
                    )

                if not point_source or len(point_source) > 64:
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {"error": "invalid_point_source", "message": "Point Source PI é obrigatório (máximo 64 caracteres)."},
                    )

                raw_enabled = data.get("enabled", True)
                enabled = bool(raw_enabled) if raw_enabled is not None else True

                with self._database.session() as repo:
                    eq = repo.get_equipment(equipment_id)
                    if eq is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})

                    existing = repo.get_pi_profile(equipment_id, opc_prog_id)
                    if existing:
                        profile_id = existing["profile_id"]
                        changed = (
                            existing["point_source"] != point_source
                            or existing["location1"] != location1
                            or bool(existing["enabled"]) != enabled
                        )
                        repo.update_pi_profile(profile_id, point_source, location1, enabled)
                        deactivated_count = 0
                        if changed:
                            deactivated_count = repo.deactivate_mappings_for_profile(
                                equipment_id, opc_prog_id, point_source, location1
                            )
                        agent_id = eq.get("agent_id")
                        if agent_id:
                            repo.add_audit_event(
                                agent_id,
                                str(uuid.uuid4()),
                                "pi_profile.updated",
                                json.dumps({
                                    "profile_id": profile_id,
                                    "equipment_id": equipment_id,
                                    "opc_prog_id": opc_prog_id,
                                    "point_source": point_source,
                                    "location1": location1,
                                    "enabled": enabled,
                                    "mappings_deactivated": deactivated_count,
                                    "user": user,
                                }),
                            )
                        updated = repo.get_pi_profile_by_id(profile_id)
                        return self._json_response(
                            start_response,
                            "200 OK",
                            {
                                "profile": updated,
                                "mappings_deactivated": deactivated_count,
                                "deactivated_mappings": deactivated_count,
                            },
                        )
                    else:
                        profile_id = str(uuid.uuid4())
                        repo.add_pi_profile(
                            profile_id=profile_id,
                            equipment_id=equipment_id,
                            opc_prog_id=opc_prog_id,
                            point_source=point_source,
                            location1=location1,
                            enabled=enabled,
                        )
                        agent_id = eq.get("agent_id")
                        if agent_id:
                            repo.add_audit_event(
                                agent_id,
                                str(uuid.uuid4()),
                                "pi_profile.created",
                                json.dumps({
                                    "profile_id": profile_id,
                                    "equipment_id": equipment_id,
                                    "opc_prog_id": opc_prog_id,
                                    "point_source": point_source,
                                    "location1": location1,
                                    "enabled": enabled,
                                    "user": user,
                                }),
                            )
                        created = repo.get_pi_profile_by_id(profile_id)
                        return self._json_response(start_response, "201 Created", {"profile": created})

            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

        # 2. Operations on /api/v1/pi-profiles/<profile_id>
        profile_id = parts[0]
        if len(parts) == 1:
            if method == "GET":
                with self._database.session() as repo:
                    prof = repo.get_pi_profile_by_id(profile_id)
                if prof is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "profile_not_found"})
                return self._json_response(start_response, "200 OK", {"profile": prof})

            if method == "DELETE":
                with self._database.session() as repo:
                    existing = repo.get_pi_profile_by_id(profile_id)
                    if existing is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "profile_not_found"})
                    repo.delete_pi_profile(profile_id)
                    repo.deactivate_mappings_for_profile(existing["equipment_id"], existing["opc_prog_id"])
                    agent_id = existing.get("agent_id")
                    if agent_id:
                        repo.add_audit_event(
                            agent_id,
                            str(uuid.uuid4()),
                            "pi_profile.deleted",
                            json.dumps({
                                "profile_id": profile_id,
                                "equipment_id": existing["equipment_id"],
                                "opc_prog_id": existing["opc_prog_id"],
                                "user": user,
                            }),
                        )
                return self._json_response(start_response, "200 OK", {"deleted": True, "profile_id": profile_id})

            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

        return self._json_response(start_response, "404 Not Found", {"error": "not_found"})

    def pi_mapping_route(
        self, method: str, path: str, query: str, environ: dict[str, Any], start_response
    ):
        if path.startswith("/api/v1/pi-integration"):
            return self.pi_integration_route(method, path, query, environ, start_response)
        if path.startswith("/api/v1/pi-profiles"):
            return self.pi_profile_route(method, path, query, environ, start_response)

        prefix = "/api/v1/pi-mappings"
        if path != prefix and not path.startswith(prefix + "/"):
            return None

        subpath = path[len(prefix):]
        parts = [p for p in subpath.split("/") if p]
        user = str(environ.get("REMOTE_USER") or "admin").strip() or "admin"

        # 1. Batch save: POST /api/v1/pi-mappings/batch
        if len(parts) == 1 and parts[0] == "batch":
            if method != "POST":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})
            try:
                data = read_json(environ)
            except Exception:
                return self._json_response(start_response, "400 Bad Request", {"error": "invalid_json"})

            equipment_id = str(data.get("equipment_id") or "").strip()
            opc_prog_id = str(data.get("opc_prog_id") or "").strip()
            rows = data.get("rows", [])
            if not isinstance(rows, list):
                return self._json_response(start_response, "400 Bad Request", {"error": "invalid_rows_format"})

            with self._database.session() as repo:
                eq = repo.get_equipment(equipment_id)
                if eq is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})

                profile = repo.get_pi_profile(equipment_id, opc_prog_id)
                if profile is None or not profile.get("enabled"):
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {
                            "error": "profile_required",
                            "message": "É obrigatório configurar e ativar o Perfil PI antes de salvar a planilha de mapeamentos.",
                        },
                    )

                # Validate rows
                row_errors: List[dict[str, Any]] = []
                seen_tags: dict[str, int] = {}
                seen_points: dict[str, int] = {}
                cleaned_rows: List[dict[str, Any]] = []

                for idx, row in enumerate(rows):
                    tag = str(row.get("opc_item_path") or "").strip()
                    point = str(row.get("pi_point_name") or "").strip()
                    raw_interval = row.get("publish_interval_ms", 1000)
                    try:
                        interval = int(raw_interval)
                    except (TypeError, ValueError):
                        interval = 0

                    if not tag:
                        row_errors.append({"row_index": idx, "field": "opc_item_path", "message": "Endereço OPC obrigatório."})
                    elif tag in seen_tags:
                        row_errors.append({
                            "row_index": idx,
                            "field": "opc_item_path",
                            "message": f"Endereço OPC duplicado na planilha (linhas {seen_tags[tag] + 1} e {idx + 1}).",
                        })
                    else:
                        seen_tags[tag] = idx

                    if not point:
                        row_errors.append({"row_index": idx, "field": "pi_point_name", "message": "PI Point obrigatório."})
                    elif point.upper() in seen_points:
                        row_errors.append({
                            "row_index": idx,
                            "field": "pi_point_name",
                            "message": f"PI Point duplicado na planilha (linhas {seen_points[point.upper()] + 1} e {idx + 1}).",
                        })
                    else:
                        seen_points[point.upper()] = idx

                    if not (1000 <= interval <= 60000):
                        row_errors.append({
                            "row_index": idx,
                            "field": "publish_interval_ms",
                            "message": "Velocidade deve ser entre 1000 e 60000 ms.",
                        })

                    cleaned_rows.append({
                        "mapping_id": str(row.get("mapping_id") or "").strip(),
                        "opc_item_path": tag,
                        "pi_point_name": point,
                        "publish_interval_ms": interval,
                        "enabled": bool(row.get("enabled", True)),
                    })

                if row_errors:
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {
                            "error": "validation_failed",
                            "message": "Existem erros de validação na planilha. Corrija as células destacadas.",
                            "row_errors": row_errors,
                        },
                    )

                saved_mappings = repo.save_pi_mappings_batch(equipment_id, opc_prog_id, cleaned_rows)

                agent_id = eq.get("agent_id")
                if agent_id:
                    repo.add_audit_event(
                        agent_id,
                        str(uuid.uuid4()),
                        "pi_mapping.batch_saved",
                        json.dumps({
                            "equipment_id": equipment_id,
                            "opc_prog_id": opc_prog_id,
                            "row_count": len(saved_mappings),
                            "user": user,
                        }),
                    )

            return self._json_response(
                start_response, "200 OK", {"saved_count": len(saved_mappings), "mappings": saved_mappings}
            )

        # 2. GET or POST /api/v1/pi-mappings
        if len(parts) == 0:
            if method == "GET":
                params = parse_qs(query) if query else {}
                equipment_id = params.get("equipment_id", [None])[0]
                opc_prog_id = params.get("opc_prog_id", [None])[0]

                with self._database.session() as repo:
                    mappings = repo.list_pi_mappings(equipment_id=equipment_id, opc_prog_id=opc_prog_id)

                augmented = []
                for m in mappings:
                    m_copy = dict(m)
                    eq_id = m.get("equipment_id", "")
                    prog_id = m.get("opc_prog_id", "")
                    tag_path = m.get("opc_item_path", "")
                    agent_id = m.get("agent_id")
                    live_it = self._get_live_item(eq_id, prog_id, tag_path, agent_id)
                    if live_it:
                        m_copy["current_value"] = live_it.get("value")
                        m_copy["quality"] = live_it.get("quality")
                        m_copy["quality_text"] = live_it.get("quality_text")
                        m_copy["opc_timestamp"] = live_it.get("opc_timestamp")
                        m_copy["stale"] = bool(live_it.get("stale", False))
                    elif m.get("last_published_value") is not None:
                        m_copy["current_value"] = m.get("last_published_value")
                        m_copy["quality"] = 192
                        m_copy["quality_text"] = "Good"
                        m_copy["opc_timestamp"] = m.get("last_published_at")
                        m_copy["stale"] = False
                    else:
                        m_copy["current_value"] = None
                        m_copy["quality"] = None
                        m_copy["quality_text"] = None
                        m_copy["opc_timestamp"] = None
                        m_copy["stale"] = False
                    augmented.append(m_copy)

                return self._json_response(start_response, "200 OK", {"mappings": augmented})

            if method == "POST":
                try:
                    data = read_json(environ)
                except Exception:
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_json"})

                equipment_id = str(data.get("equipment_id") or "").strip()
                opc_prog_id = str(data.get("opc_prog_id") or "").strip()
                opc_item_path = str(data.get("opc_item_path") or "").strip()

                if not equipment_id or not opc_prog_id or not opc_item_path:
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {"error": "missing_required_fields", "message": "Equipamento, Servidor OPC e Endereço OPC são obrigatórios."},
                    )

                with self._database.session() as repo:
                    eq = repo.get_equipment(equipment_id)
                    if eq is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "equipment_not_found"})

                    profile = repo.get_pi_profile(equipment_id, opc_prog_id)
                    if profile is None or not profile.get("enabled"):
                        return self._json_response(
                            start_response,
                            "400 Bad Request",
                            {
                                "error": "profile_required",
                                "message": "É obrigatório configurar e ativar o Perfil PI para este Equipamento e Servidor OPC antes de cadastrar mapeamentos.",
                            },
                        )

                    try:
                        validated = validate_pi_mapping_input(data, profile=profile)
                    except ValueError as exc:
                        return self._json_response(start_response, "400 Bad Request", {"error": str(exc)})

                    # Check for duplicate mapping
                    existing_list = repo.list_pi_mappings(equipment_id=equipment_id, opc_prog_id=opc_prog_id)
                    for em in existing_list:
                        if em.get("opc_item_path") == opc_item_path:
                            return self._json_response(
                                start_response,
                                "409 Conflict",
                                {"error": "mapping_conflict", "message": f"Já existe um mapeamento cadastrado para o endereço '{opc_item_path}' neste perfil."},
                            )
                        if em.get("pi_point_name", "").upper() == validated["pi_point_name"].upper():
                            return self._json_response(
                                start_response,
                                "409 Conflict",
                                {"error": "point_conflict", "message": f"Já existe um mapeamento cadastrado para o PI Point '{validated['pi_point_name']}' neste perfil."},
                            )

                    mapping_id = str(uuid.uuid4())
                    repo.add_pi_mapping(
                        mapping_id=mapping_id,
                        equipment_id=equipment_id,
                        opc_prog_id=opc_prog_id,
                        opc_item_path=opc_item_path,
                        pi_point_name=validated["pi_point_name"],
                        point_source=profile["point_source"],
                        location1=profile["location1"],
                        publish_interval_ms=validated["publish_interval_ms"],
                        enabled=validated["enabled"],
                        profile_id=profile["profile_id"],
                    )

                    agent_id = eq.get("agent_id")
                    if agent_id:
                        repo.add_audit_event(
                            agent_id,
                            str(uuid.uuid4()),
                            "pi_mapping.created",
                            json.dumps({
                                "mapping_id": mapping_id,
                                "equipment_id": equipment_id,
                                "opc_prog_id": opc_prog_id,
                                "opc_item_path": opc_item_path,
                                "pi_point_name": validated["pi_point_name"],
                                "point_source": profile["point_source"],
                                "location1": profile["location1"],
                                "publish_interval_ms": validated["publish_interval_ms"],
                                "enabled": validated["enabled"],
                                "user": user,
                            }),
                        )

                    mapping_dict = repo.get_pi_mapping(mapping_id)

                return self._json_response(start_response, "201 Created", {"mapping": mapping_dict})

            return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

        # Audit events query: GET /api/v1/pi-mappings/audit
        if len(parts) == 1 and parts[0] == "audit":
            if method != "GET":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

            params = parse_qs(query) if query else {}
            equipment_id = params.get("equipment_id", [None])[0]
            limit_raw = params.get("limit", [50])[0]
            try:
                limit = int(limit_raw)
            except (ValueError, TypeError):
                limit = 50

            with self._database.session() as repo:
                agent_id = None
                if equipment_id:
                    eq = repo.get_equipment(equipment_id)
                    if eq:
                        agent_id = eq.get("agent_id")

                query_sql = (
                    "SELECT event_id, agent_id, event_type, detail_json, occurred_at "
                    "FROM audit_events WHERE (event_type LIKE ? OR event_type LIKE ?) "
                )
                sql_params = ["pi_mapping.%", "pi_profile.%"]
                if agent_id:
                    query_sql += "AND agent_id = ? "
                    sql_params.append(agent_id)
                query_sql += f"ORDER BY occurred_at DESC LIMIT {max(1, min(limit, 200))}"
                rows = repo._dicts(repo._execute(query_sql, tuple(sql_params)))

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

        # 3. Operations on specific mapping: /api/v1/pi-mappings/<id>[/action]
        mapping_id = parts[0]

        # Simulation: POST /api/v1/pi-mappings/<id>/simulate
        if len(parts) == 2 and parts[1] == "simulate":
            if method != "POST":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

            with self._database.session() as repo:
                mapping = repo.get_pi_mapping(mapping_id)
                if mapping is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})

                profile = repo.get_pi_profile(mapping["equipment_id"], mapping["opc_prog_id"])
                if profile is None or not profile.get("enabled"):
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {
                            "error": "profile_required",
                            "message": "Não é possível simular o mapeamento sem um Perfil PI ativo para esta configuração.",
                        },
                    )

                if not mapping.get("enabled"):
                    return self._json_response(
                        start_response,
                        "409 Conflict",
                        {"error": "mapping_disabled", "message": "O mapeamento está desativado e não pode ser publicado no PI."},
                    )

                agent_id = mapping.get("agent_id")
                opc_path = mapping.get("opc_item_path")

                # Retrieve last reading from PI cache or active live collection
                live_item = self._get_live_item(
                    mapping["equipment_id"], mapping["opc_prog_id"], opc_path, agent_id
                )

                current_val = live_item.get("value") if live_item else None
                opc_ts = live_item.get("opc_timestamp") if live_item else None
                quality = live_item.get("quality") if live_item else None

                channel = self._get_pi_output_channel()
                sim_res = channel.publish(
                    pi_point_name=mapping["pi_point_name"],
                    value=current_val if current_val is not None else 0.0,
                    timestamp=opc_ts,
                    quality=quality,
                    point_source=profile["point_source"],
                    location1=profile["location1"],
                )

                val_str = str(current_val) if current_val is not None else "0.0"
                repo.update_pi_mapping_status(mapping_id, sim_res.status, val_str)

                if agent_id:
                    repo.add_audit_event(
                        agent_id,
                        str(uuid.uuid4()),
                        "pi_mapping.simulation",
                        json.dumps({
                            "mapping_id": mapping_id,
                            "opc_item_path": opc_path,
                            "pi_point_name": mapping["pi_point_name"],
                            "point_source": profile["point_source"],
                            "location1": profile["location1"],
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

                profile = repo.get_pi_profile(mapping["equipment_id"], mapping["opc_prog_id"])
                if profile is None or not profile.get("enabled"):
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {
                            "error": "profile_required",
                            "message": "Não é possível publicar o mapeamento sem um Perfil PI ativo para esta configuração.",
                        },
                    )

                if not mapping.get("enabled"):
                    return self._json_response(
                        start_response,
                        "409 Conflict",
                        {"error": "mapping_disabled", "message": "O mapeamento está desativado e não pode ser publicado no PI."},
                    )

                agent_id = mapping.get("agent_id")
                opc_path = mapping.get("opc_item_path")

                live_item = self._get_live_item(
                    mapping["equipment_id"], mapping["opc_prog_id"], opc_path, agent_id
                )

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
                    reason = eval_res.get("reason", "skipped")
                    msg_map = {
                        "no_value": "Nenhum valor recente disponível na leitura OPC.",
                        "quality_not_good": f"Qualidade do valor OPC ({eval_res.get('quality')}) não é Good (>= 192).",
                        "stale_data": "O dado no cache OPC está obsoleto.",
                        "mapping_disabled": "Mapeamento desativado.",
                    }
                    msg = msg_map.get(reason, f"Publicação não executada ({reason}).")
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
                            "point_source": profile["point_source"],
                            "location1": profile["location1"],
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
                if new_enabled:
                    profile = repo.get_pi_profile(mapping["equipment_id"], mapping["opc_prog_id"])
                    if profile is None or not profile.get("enabled"):
                        return self._json_response(
                            start_response,
                            "400 Bad Request",
                            {
                                "error": "profile_required",
                                "message": "Não é possível ativar o mapeamento sem um Perfil PI ativo para este servidor OPC.",
                            },
                        )

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

        # Validate PI Point: POST /api/v1/pi-mappings/<id>/validate-point
        if len(parts) == 2 and parts[1] == "validate-point":
            if method != "POST":
                return self._json_response(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

            with self._database.session() as repo:
                mapping = repo.get_pi_mapping(mapping_id)
                if mapping is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})

                profile = repo.get_pi_profile(mapping["equipment_id"], mapping["opc_prog_id"])
                if profile is None or not profile.get("enabled"):
                    return self._json_response(
                        start_response,
                        "400 Bad Request",
                        {
                            "error": "profile_required",
                            "message": "Não é possível validar o PI Point sem um Perfil PI ativo para esta configuração.",
                        },
                    )

                channel = self._get_pi_output_channel()
                val_res = channel.validate_point(
                    pi_point_name=mapping["pi_point_name"],
                    point_source=profile["point_source"],
                    location1=profile["location1"],
                )

                agent_id = mapping.get("agent_id")
                if agent_id:
                    repo.add_audit_event(
                        agent_id,
                        str(uuid.uuid4()),
                        "pi_mapping.validated",
                        json.dumps({
                            "mapping_id": mapping_id,
                            "pi_point_name": mapping["pi_point_name"],
                            "point_source": profile["point_source"],
                            "location1": profile["location1"],
                            "valid": val_res.get("valid"),
                            "status": val_res.get("message") or val_res.get("error"),
                            "user": user,
                        }),
                    )

                status_code = "200 OK" if val_res.get("valid") else "400 Bad Request"
                resp = dict(val_res)
                resp["result"] = val_res
                resp["mapping"] = mapping
                return self._json_response(start_response, status_code, resp)

        # Single mapping operations: GET, PUT, DELETE /api/v1/pi-mappings/<id>
        if len(parts) == 1:
            if method == "GET":
                with self._database.session() as repo:
                    mapping = repo.get_pi_mapping(mapping_id)
                if mapping is None:
                    return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})
                m_copy = dict(mapping)
                live_it = self._get_live_item(
                    mapping.get("equipment_id", ""),
                    mapping.get("opc_prog_id", ""),
                    mapping.get("opc_item_path", ""),
                    mapping.get("agent_id"),
                )
                if live_it:
                    m_copy["current_value"] = live_it.get("value")
                    m_copy["quality"] = live_it.get("quality")
                    m_copy["quality_text"] = live_it.get("quality_text")
                    m_copy["opc_timestamp"] = live_it.get("opc_timestamp")
                    m_copy["stale"] = bool(live_it.get("stale", False))
                elif mapping.get("last_published_value") is not None:
                    m_copy["current_value"] = mapping.get("last_published_value")
                    m_copy["quality"] = 192
                    m_copy["quality_text"] = "Good"
                    m_copy["opc_timestamp"] = mapping.get("last_published_at")
                    m_copy["stale"] = False
                else:
                    m_copy["stale"] = False
                return self._json_response(start_response, "200 OK", {"mapping": m_copy})

            if method in ("PUT", "POST"):
                try:
                    data = read_json(environ)
                except Exception:
                    return self._json_response(start_response, "400 Bad Request", {"error": "invalid_json"})

                with self._database.session() as repo:
                    existing = repo.get_pi_mapping(mapping_id)
                    if existing is None:
                        return self._json_response(start_response, "404 Not Found", {"error": "mapping_not_found"})

                    profile = repo.get_pi_profile(existing["equipment_id"], existing["opc_prog_id"])
                    raw_en = data.get("enabled", True)
                    if bool(raw_en) and (profile is None or not profile.get("enabled")):
                        return self._json_response(
                            start_response,
                            "400 Bad Request",
                            {
                                "error": "profile_required",
                                "message": "Não é possível ativar o mapeamento sem um Perfil PI ativo para esta configuração.",
                            },
                        )

                    try:
                        validated = validate_pi_mapping_input(data, profile=profile)
                    except ValueError as exc:
                        return self._json_response(start_response, "400 Bad Request", {"error": str(exc)})

                    new_path = str(data.get("opc_item_path") or existing["opc_item_path"]).strip()
                    repo.update_pi_mapping(
                        mapping_id=mapping_id,
                        opc_item_path=new_path,
                        pi_point_name=validated["pi_point_name"],
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
                                "opc_item_path": new_path,
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
