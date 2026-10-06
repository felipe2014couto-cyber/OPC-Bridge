"""Agent-side execution and strict security gating for OPC write operations."""
from __future__ import annotations

import logging
from typing import Any

from opc_bridge.protocol.write import (
    WRITES_DISABLED_MESSAGE,
    WriteItemResult,
    WriteRequest,
    WriteResponse,
    is_writes_enabled,
    validate_item_value,
)

logger = logging.getLogger(__name__)


def execute_agent_write(
    request: WriteRequest,
    adapter: Any | None,
    write_allowlist: set[str] | dict[str, Any] | list[str] | None = None,
    allowlist: Any = None,
) -> WriteResponse:
    """Execute a write request with double-gated authorization:
    1. Global kill switch (OPC_BRIDGE_ENABLE_WRITES=true).
    2. Per-tag allowlist (write_enabled=true).
    3. Type and bounds validation.
    4. Safe adapter write execution (or simulated write).
    """
    if write_allowlist is None and allowlist is not None:
        write_allowlist = allowlist

    # 1. Global kill switch check
    if not is_writes_enabled():
        logger.warning("Write rejected: %s", WRITES_DISABLED_MESSAGE)
        return WriteResponse(
            request_id=request.request_id,
            error=WRITES_DISABLED_MESSAGE,
            results=[
                WriteItemResult(
                    tag=item.tag,
                    status="rejected",
                    value=None,
                    error=WRITES_DISABLED_MESSAGE,
                )
                for item in request.items
            ],
        )

    # 2. Adapter availability check
    if adapter is None or not getattr(adapter, "is_connected", False):
        logger.warning("Write failed: adapter is not connected")
        return WriteResponse(
            request_id=request.request_id,
            error="adapter_unavailable",
            results=[
                WriteItemResult(
                    tag=item.tag,
                    status="error",
                    value=None,
                    error="OPC adapter is not connected",
                )
                for item in request.items
            ],
        )

    results: list[WriteItemResult] = []
    items_to_write: list[tuple[str, Any]] = []
    index_map: dict[str, int] = {}

    for item in request.items:
        # 3. Per-tag authorization check
        is_authorized = True
        if write_allowlist is not None:
            if isinstance(write_allowlist, (set, list, tuple)):
                is_authorized = item.tag in write_allowlist
            elif isinstance(write_allowlist, dict):
                spec = write_allowlist.get(item.tag)
                if spec is None:
                    is_authorized = False
                elif isinstance(spec, dict):
                    is_authorized = bool(spec.get("write_enabled", False))
                elif isinstance(spec, bool):
                    is_authorized = spec
                else:
                    is_authorized = bool(spec)

        if not is_authorized:
            logger.warning("Write rejected for unauthorized tag: %s", item.tag)
            results.append(
                WriteItemResult(
                    tag=item.tag,
                    status="rejected",
                    value=None,
                    error=f"Tag '{item.tag}' is not authorized for write operations",
                )
            )
            continue

        # 4. Type & bounds validation
        valid, coerced, err = validate_item_value(item.value, item.data_type)
        if not valid:
            logger.warning("Write rejected for invalid value on tag %s: %s", item.tag, err)
            results.append(
                WriteItemResult(
                    tag=item.tag,
                    status="rejected",
                    value=None,
                    error=err or "Invalid value",
                )
            )
            continue

        # Validated and authorized; queue for write
        items_to_write.append((item.tag, coerced))
        index_map[item.tag] = len(results)
        results.append(
            WriteItemResult(
                tag=item.tag,
                status="pending",
                value=coerced,
                error=None,
            )
        )

    # 5. Execute writes on the adapter if any items are queued
    if items_to_write:
        if not hasattr(adapter, "write_items"):
            logger.error("Adapter does not support write_items")
            for tag, _ in items_to_write:
                idx = index_map[tag]
                results[idx].status = "error"
                results[idx].error = "Adapter does not support write operations"
        else:
            try:
                write_outcomes = adapter.write_items(items_to_write)
                for tag, success, write_err in write_outcomes:
                    if tag in index_map:
                        idx = index_map[tag]
                        if success:
                            results[idx].status = "applied"
                            results[idx].error = None
                        else:
                            results[idx].status = "error"
                            results[idx].error = write_err or "Write failed"
            except Exception as exc:
                logger.exception("Exception during adapter.write_items: %s", exc)
                for tag, _ in items_to_write:
                    idx = index_map[tag]
                    results[idx].status = "error"
                    results[idx].error = f"Adapter error: {type(exc).__name__}"

    return WriteResponse(
        request_id=request.request_id,
        error=None,
        results=results,
    )
