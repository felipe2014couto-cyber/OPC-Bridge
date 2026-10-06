"""Non-destructive inspection using a separate, short-lived OPC adapter."""
from __future__ import annotations

import logging
import threading
import time
import uuid

from opc_bridge.adapters.base import opc_hresult
from opc_bridge.protocol.inspection import InspectionRequest, InspectionResponse, valid_text
from opc_bridge.protocol.messages import (
    ItemStatus,
    decode_value,
    format_opc_timestamp,
    format_quality_text,
    sanitize_error,
)

_slot = threading.BoundedSemaphore(1)
logger = logging.getLogger(__name__)


def inspect_opc(request: InspectionRequest, adapter_factory, active_adapter) -> InspectionResponse:
    if not _slot.acquire(blocking=False):
        return InspectionResponse(request.request_id, error="busy")
    candidate = None
    group = None
    is_reused = False
    t_total_start = time.perf_counter()
    t_wait_ms = 0.0
    t_worker_ms = 0.0
    t_group_ms = 0.0
    t_add_ms = 0.0
    t_read_ms = 0.0
    t_cleanup_ms = 0.0
    try:
        req_prog = (request.opc_prog_id or "").strip()
        active_prog = (getattr(active_adapter, "prog_id", None) or getattr(active_adapter, "_prog_id", None) or "")
        if isinstance(active_prog, str):
            active_prog = active_prog.strip()
        else:
            active_prog = str(active_prog).strip() if active_prog else ""

        active_connected = getattr(active_adapter, "is_connected", None)
        if callable(active_connected):
            active_connected = active_connected()
        elif active_connected is None:
            active_connected = getattr(active_adapter, "_connected", False)
            if hasattr(active_adapter, "is_alive") and not active_adapter.is_alive:
                active_connected = False

        can_reuse = bool(
            request.action == "tags"
            and active_adapter is not None
            and active_connected
            and req_prog
            and active_prog
            and req_prog.lower() == active_prog.lower()
        )

        t_worker_start = time.perf_counter()
        if can_reuse:
            candidate = active_adapter
            is_reused = True
            t_worker_ms = (time.perf_counter() - t_worker_start) * 1000
        else:
            if adapter_factory is None:
                return InspectionResponse(request.request_id, error="unavailable")
            candidate = adapter_factory()
            if candidate is active_adapter:
                candidate = None
                return InspectionResponse(request.request_id, error="unavailable")
            is_reused = False
            # Limit only the temporary worker; never change the active worker's deadlines.
            if hasattr(candidate, "command_timeout"):
                candidate.command_timeout = min(candidate.command_timeout, 5)
                candidate.connect_timeout = min(candidate.connect_timeout, 10)
                candidate.startup_timeout = 10
            if request.action == "servers":
                servers = candidate.discover_servers()
                t_worker_ms = (time.perf_counter() - t_worker_start) * 1000
                return InspectionResponse(
                    request.request_id,
                    servers=sorted({p for p in servers if valid_text(p, 256)})[:100],
                )
            candidate.connect(request.opc_prog_id)
            t_worker_ms = (time.perf_counter() - t_worker_start) * 1000

        deadline = time.monotonic() + 30
        t_group_start = time.perf_counter()
        group = candidate.create_group("inspection_" + uuid.uuid4().hex, 1000)
        t_group_ms = (time.perf_counter() - t_group_start) * 1000
        tags = request.tags or []
        results: list[dict | None] = [None] * len(tags)
        valid_items_to_read: list[tuple[int, str, int]] = []  # (index, path, item_id)

        t_add_start = time.perf_counter()

        for idx, path in enumerate(tags):
            if time.monotonic() >= deadline:
                results[idx] = {
                    "opc_item_path": path,
                    "status": "error",
                    "hresult": None,
                    "value": None,
                    "value_type": None,
                    "quality": None,
                    "quality_text": None,
                    "opc_timestamp": None,
                    "error": "A inspeção excedeu o prazo.",
                }
                continue
            try:
                mapping = candidate.add_items(group, [path])
                item_id = mapping.get(path) if isinstance(mapping, dict) else None
                if not (type(item_id) is int and item_id > 0):
                    results[idx] = {
                        "opc_item_path": path,
                        "status": "invalid",
                        "hresult": 0xC0040007,
                        "value": None,
                        "value_type": None,
                        "quality": None,
                        "quality_text": None,
                        "opc_timestamp": None,
                        "error": "Endereço OPC não encontrado.",
                    }
                    continue
                valid_items_to_read.append((idx, path, item_id))
            except Exception as exc:  # noqa: BLE001 - COM provider boundary.
                if isinstance(exc, TimeoutError):
                    deadline = 0
                hresult = opc_hresult(exc)
                if hresult is None or hresult == 0x80020009:
                    exc_str = str(exc)
                    for code in (0xC0040007, 0xC0040008, 0x80040001):
                        signed_code = code if code < 0x80000000 else code - 0x100000000
                        if (
                            f"{code:08X}" in exc_str.upper()
                            or hex(code).upper() in exc_str.upper()
                            or str(signed_code) in exc_str
                            or str(code) in exc_str
                        ):
                            hresult = code
                            break
                if hresult in (0xC0040007, 0xC0040008, 0x80040001):
                    status = "invalid"
                    err_str = "Endereço OPC não encontrado."
                else:
                    status = "error"
                    err_str = sanitize_error(hresult or 0, ItemStatus.ERROR) if hresult else "Erro na operação OPC."
                results[idx] = {
                    "opc_item_path": path,
                    "status": status,
                    "hresult": hresult,
                    "value": None,
                    "value_type": None,
                    "quality": None,
                    "quality_text": None,
                    "opc_timestamp": None,
                    "error": err_str,
                }
        t_add_ms = (time.perf_counter() - t_add_start) * 1000

        t_read_start = time.perf_counter()
        if valid_items_to_read:
            if time.monotonic() >= deadline:
                for idx, path, _ in valid_items_to_read:
                    results[idx] = {
                        "opc_item_path": path,
                        "status": "error",
                        "hresult": None,
                        "value": None,
                        "value_type": None,
                        "quality": None,
                        "quality_text": None,
                        "opc_timestamp": None,
                        "error": "A inspeção excedeu o prazo.",
                    }
            else:
                item_ids = [item_id for _, _, item_id in valid_items_to_read]
                try:
                    read_results = candidate.read_device(group, item_ids)
                    results_by_id = {}
                    if read_results:
                        for res in read_results:
                            results_by_id[res.item_id] = res

                    for idx, path, item_id in valid_items_to_read:
                        res = results_by_id.get(item_id)
                        if res is None:
                            results[idx] = {
                                "opc_item_path": path,
                                "status": "error",
                                "hresult": None,
                                "value": None,
                                "value_type": None,
                                "quality": None,
                                "quality_text": None,
                                "opc_timestamp": None,
                                "error": "Falha na leitura do dispositivo.",
                            }
                            continue

                        if res.status == ItemStatus.NOT_FOUND or res.error_code in (0xC0040007, 0xC0040008, 0x80040001):
                            results[idx] = {
                                "opc_item_path": path,
                                "status": "invalid",
                                "hresult": (res.error_code & 0xFFFFFFFF) if res.error_code else 0xC0040007,
                                "value": None,
                                "value_type": None,
                                "quality": None,
                                "quality_text": None,
                                "opc_timestamp": None,
                                "error": "Endereço OPC não encontrado.",
                            }
                        elif res.error_code != 0 or res.status in (ItemStatus.ERROR, ItemStatus.TIMEOUT):
                            err_str = sanitize_error(res.error_code, res.status)
                            results[idx] = {
                                "opc_item_path": path,
                                "status": "error",
                                "hresult": (res.error_code & 0xFFFFFFFF) if res.error_code != 0 else None,
                                "value": None,
                                "value_type": None,
                                "quality": res.quality if res.quality is not None else None,
                                "quality_text": format_quality_text(res.quality) if res.quality is not None else None,
                                "opc_timestamp": format_opc_timestamp(res.timestamp_us),
                                "error": err_str or "Erro de leitura OPC.",
                            }
                        else:
                            val, val_type_name = decode_value(res.value_type, res.value)
                            results[idx] = {
                                "opc_item_path": path,
                                "status": "valid",
                                "hresult": None,
                                "value": val,
                                "value_type": val_type_name,
                                "quality": res.quality,
                                "quality_text": format_quality_text(res.quality),
                                "opc_timestamp": format_opc_timestamp(res.timestamp_us),
                                "error": None,
                            }
                except Exception as exc:  # noqa: BLE001 - COM provider boundary.
                    hresult = opc_hresult(exc)
                    if hresult in (0xC0040007, 0xC0040008, 0x80040001):
                        batch_status = "invalid"
                        batch_err = "Endereço OPC não encontrado."
                    else:
                        batch_status = "error"
                        batch_err = sanitize_error(hresult or 0, ItemStatus.ERROR) if hresult else "Falha na leitura do dispositivo."
                    for idx, path, _ in valid_items_to_read:
                        results[idx] = {
                            "opc_item_path": path,
                            "status": batch_status,
                            "hresult": hresult,
                            "value": None,
                            "value_type": None,
                            "quality": None,
                            "quality_text": None,
                            "opc_timestamp": None,
                            "error": batch_err,
                        }
        t_read_ms = (time.perf_counter() - t_read_start) * 1000

        final_results = [r for r in results if r is not None]
        return InspectionResponse(request.request_id, results=final_results)
    except Exception as exc:  # noqa: BLE001 - never send provider messages or COM objects.
        hres = opc_hresult(exc)
        err_msg = sanitize_error(hres or 0) if hres else "Falha na inspeção OPC."
        results = [
            {
                "opc_item_path": path,
                "status": "error",
                "hresult": hres,
                "value": None,
                "value_type": None,
                "quality": None,
                "quality_text": None,
                "opc_timestamp": None,
                "error": err_msg,
            }
            for path in request.tags or []
        ]
        return InspectionResponse(request.request_id, results=results, error="inspection_failed")
    finally:
        t_cleanup_start = time.perf_counter()
        if group is not None and candidate is not None:
            try:
                candidate.remove_group(group)
            except Exception:  # noqa: BLE001 - best-effort temporary group cleanup.
                logger.warning("Failed to remove temporary inspection group %s", getattr(group, "name", group))
        if not is_reused and candidate is not None:
            try:
                candidate.disconnect()
            except Exception:  # noqa: BLE001 - best-effort temporary worker cleanup.
                logger.warning("Temporary OPC inspection cleanup failed")
        t_cleanup_ms = (time.perf_counter() - t_cleanup_start) * 1000
        t_total_ms = (time.perf_counter() - t_total_start) * 1000
        logger.info(
            "Inspection duration [mode=%s]: wait=%.2fms worker=%.2fms group=%.2fms add=%.2fms read=%.2fms cleanup=%.2fms total=%.2fms",
            "reused" if is_reused else "fallback",
            t_wait_ms,
            t_worker_ms,
            t_group_ms,
            t_add_ms,
            t_read_ms,
            t_cleanup_ms,
            t_total_ms,
        )
        _slot.release()
