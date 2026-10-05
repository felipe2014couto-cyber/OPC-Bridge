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
    if adapter_factory is None:
        return InspectionResponse(request.request_id, error="unavailable")
    if not _slot.acquire(blocking=False):
        return InspectionResponse(request.request_id, error="busy")
    candidate = None
    try:
        candidate = adapter_factory()
        if candidate is active_adapter:
            candidate = None
            return InspectionResponse(request.request_id, error="unavailable")
        # Limit only the temporary worker; never change the active worker's deadlines.
        if hasattr(candidate, "command_timeout"):
            candidate.command_timeout = min(candidate.command_timeout, 5)
            candidate.connect_timeout = min(candidate.connect_timeout, 10)
            candidate.startup_timeout = 10
        deadline = time.monotonic() + 30
        if request.action == "servers":
            servers = candidate.discover_servers()
            return InspectionResponse(
                request.request_id,
                servers=sorted({p for p in servers if valid_text(p, 256)})[:100],
            )
        candidate.connect(request.opc_prog_id)
        group = candidate.create_group("inspection_" + uuid.uuid4().hex, 1000)
        results = []
        for path in request.tags or []:
            if time.monotonic() >= deadline:
                results.append({
                    "opc_item_path": path,
                    "status": "error",
                    "hresult": None,
                    "value": None,
                    "value_type": None,
                    "quality": None,
                    "quality_text": None,
                    "opc_timestamp": None,
                    "error": "A inspeção excedeu o prazo.",
                })
                continue
            try:
                mapping = candidate.add_items(group, [path])
                item_id = mapping.get(path) if isinstance(mapping, dict) else None
                if not (type(item_id) is int and item_id > 0):
                    results.append({
                        "opc_item_path": path,
                        "status": "invalid",
                        "hresult": 0xC0040007,
                        "value": None,
                        "value_type": None,
                        "quality": None,
                        "quality_text": None,
                        "opc_timestamp": None,
                        "error": "Endereço OPC não encontrado.",
                    })
                    continue

                read_results = candidate.read_device(group, [item_id])
                if not read_results:
                    results.append({
                        "opc_item_path": path,
                        "status": "error",
                        "hresult": None,
                        "value": None,
                        "value_type": None,
                        "quality": None,
                        "quality_text": None,
                        "opc_timestamp": None,
                        "error": "Falha na leitura do dispositivo.",
                    })
                    continue

                res = read_results[0]
                if res.status == ItemStatus.NOT_FOUND or res.error_code in (0xC0040007, 0xC0040008, 0x80040001):
                    results.append({
                        "opc_item_path": path,
                        "status": "invalid",
                        "hresult": (res.error_code & 0xFFFFFFFF) if res.error_code else 0xC0040007,
                        "value": None,
                        "value_type": None,
                        "quality": None,
                        "quality_text": None,
                        "opc_timestamp": None,
                        "error": "Endereço OPC não encontrado.",
                    })
                elif res.error_code != 0 or res.status in (ItemStatus.ERROR, ItemStatus.TIMEOUT):
                    err_str = sanitize_error(res.error_code, res.status)
                    results.append({
                        "opc_item_path": path,
                        "status": "error",
                        "hresult": (res.error_code & 0xFFFFFFFF) if res.error_code != 0 else None,
                        "value": None,
                        "value_type": None,
                        "quality": res.quality if res.quality is not None else None,
                        "quality_text": format_quality_text(res.quality) if res.quality is not None else None,
                        "opc_timestamp": format_opc_timestamp(res.timestamp_us),
                        "error": err_str or "Erro de leitura OPC.",
                    })
                else:
                    val, val_type_name = decode_value(res.value_type, res.value)
                    results.append({
                        "opc_item_path": path,
                        "status": "valid",
                        "hresult": None,
                        "value": val,
                        "value_type": val_type_name,
                        "quality": res.quality,
                        "quality_text": format_quality_text(res.quality),
                        "opc_timestamp": format_opc_timestamp(res.timestamp_us),
                        "error": None,
                    })
            except Exception as exc:  # noqa: BLE001 - COM provider boundary.
                if isinstance(exc, TimeoutError):
                    deadline = 0
                hresult = opc_hresult(exc)
                if hresult is None:
                    exc_str = str(exc).upper()
                    for code in (0xC0040007, 0xC0040008, 0x80040001):
                        if f"{code:08X}" in exc_str or hex(code).upper() in exc_str:
                            hresult = code
                            break
                if hresult in (0xC0040007, 0xC0040008, 0x80040001):
                    status = "invalid"
                    err_str = "Endereço OPC não encontrado."
                else:
                    status = "error"
                    err_str = sanitize_error(hresult or 0, ItemStatus.ERROR) if hresult else "Erro na operação OPC."
                results.append({
                    "opc_item_path": path,
                    "status": status,
                    "hresult": hresult,
                    "value": None,
                    "value_type": None,
                    "quality": None,
                    "quality_text": None,
                    "opc_timestamp": None,
                    "error": err_str,
                })
        return InspectionResponse(request.request_id, results=results)
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
        try:
            if candidate is not None:
                try:
                    candidate.disconnect()
                except Exception:  # noqa: BLE001 - best-effort temporary worker cleanup.
                    logger.warning("Temporary OPC inspection cleanup failed")
        finally:
            _slot.release()
