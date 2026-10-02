"""Non-destructive inspection using a separate, short-lived OPC adapter."""
from __future__ import annotations

import logging
import threading
import time
import uuid

from opc_bridge.adapters.base import opc_hresult
from opc_bridge.protocol.inspection import InspectionRequest, InspectionResponse, valid_text

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
                results.append({"opc_item_path": path, "status": "error", "hresult": None})
                continue
            try:
                mapping = candidate.add_items(group, [path])
                status = "valid" if type(mapping.get(path)) is int and mapping[path] > 0 else "invalid"
                results.append({"opc_item_path": path, "status": status, "hresult": None})
            except Exception as exc:  # noqa: BLE001 - COM provider boundary.
                if isinstance(exc, TimeoutError):
                    deadline = 0
                hresult = opc_hresult(exc)
                status = "invalid" if hresult in (0xC0040007, 0xC0040008) else "error"
                results.append({"opc_item_path": path, "status": status, "hresult": hresult})
        return InspectionResponse(request.request_id, results=results)
    except Exception as exc:  # noqa: BLE001 - never send provider messages or COM objects.
        results = [{"opc_item_path": path, "status": "error", "hresult": opc_hresult(exc)}
                   for path in request.tags or []]
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
