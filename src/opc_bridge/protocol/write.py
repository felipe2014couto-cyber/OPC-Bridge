"""Bounded, primitive-only OPC write operation protocol messages and validation."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from opc_bridge.protocol.inspection import valid_text

CAPABILITY_WRITE = "opc-write-v1"
MAX_WRITE_ITEMS = 50
ALLOWED_DATA_TYPES = {"boolean", "integer", "float", "string"}
WRITES_DISABLED_MESSAGE = "Writes are globally disabled on this environment (OPC_BRIDGE_ENABLE_WRITES=false)"


def is_writes_enabled() -> bool:
    """Global kill switch: defaults to False unless OPC_BRIDGE_ENABLE_WRITES is explicitly 'true'."""
    return os.environ.get("OPC_BRIDGE_ENABLE_WRITES", "false").strip().lower() in ("true", "1", "yes")


def validate_item_value(
    raw_value: Any,
    data_type: str,
    min_val: float | None = None,
    max_val: float | None = None,
    allowed_values: list[Any] | None = None,
    **kwargs: Any,
) -> tuple[bool, Any, str | None]:
    """Validate and safely coerce a value according to its type, bounds, and allowed list.

    Returns (is_valid, coerced_value, error_message).
    """
    if min_val is None and "min_value" in kwargs:
        min_val = kwargs["min_value"]
    if max_val is None and "max_value" in kwargs:
        max_val = kwargs["max_value"]

    if data_type not in ALLOWED_DATA_TYPES:
        return False, None, f"Unsupported data type: '{data_type}'"

    coerced: Any = None

    if data_type == "boolean":
        if isinstance(raw_value, bool):
            coerced = raw_value
        elif isinstance(raw_value, (int, float)) and raw_value in (0, 1):
            coerced = bool(raw_value)
        elif isinstance(raw_value, str):
            normalized = raw_value.strip().lower()
            if normalized in ("true", "1", "sim", "yes", "t", "s"):
                coerced = True
            elif normalized in ("false", "0", "nao", "não", "no", "f", "n"):
                coerced = False
            else:
                return False, None, f"Cannot convert '{raw_value}' to boolean"
        else:
            return False, None, f"Cannot convert value of type {type(raw_value).__name__} to boolean"

    elif data_type == "integer":
        if isinstance(raw_value, bool):
            return False, None, "Boolean is not accepted as integer"
        if isinstance(raw_value, int):
            coerced = raw_value
        elif isinstance(raw_value, float):
            if raw_value.is_integer():
                coerced = int(raw_value)
            else:
                return False, None, f"Float '{raw_value}' has fractional part, expected integer"
        elif isinstance(raw_value, str):
            try:
                coerced = int(raw_value.strip())
            except ValueError:
                return False, None, f"Cannot convert '{raw_value}' to integer"
        else:
            return False, None, f"Cannot convert value of type {type(raw_value).__name__} to integer"

        if min_val is not None and coerced < min_val:
            return False, None, f"Value {coerced} is below minimum limit {min_val}"
        if max_val is not None and coerced > max_val:
            return False, None, f"Value {coerced} exceeds maximum limit {max_val}"

    elif data_type == "float":
        if isinstance(raw_value, bool):
            return False, None, "Boolean is not accepted as float"
        if isinstance(raw_value, (int, float)):
            coerced = float(raw_value)
        elif isinstance(raw_value, str):
            try:
                coerced = float(raw_value.strip().replace(",", "."))
            except ValueError:
                return False, None, f"Cannot convert '{raw_value}' to float"
        else:
            return False, None, f"Cannot convert value of type {type(raw_value).__name__} to float"

        if min_val is not None and coerced < min_val:
            return False, None, f"Value {coerced} is below minimum limit {min_val}"
        if max_val is not None and coerced > max_val:
            return False, None, f"Value {coerced} exceeds maximum limit {max_val}"

    elif data_type == "string":
        if isinstance(raw_value, (str, int, float, bool)):
            str_val = str(raw_value)
            if len(str_val) > 1024:
                return False, None, "String exceeds 1024 character limit"
            coerced = str_val
        else:
            return False, None, f"Value must be convertible to string, got {type(raw_value).__name__}"

    if allowed_values is not None and len(allowed_values) > 0:
        if coerced not in allowed_values:
            return False, None, f"Value {coerced} is not in allowed values: {allowed_values}"

    return True, coerced, None


def _decode(raw: bytes) -> dict:
    if len(raw) > 131072:
        raise ValueError("Write payload too large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate write payload field")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except RecursionError:
        raise ValueError("Invalid write payload nesting") from None
    if not isinstance(value, dict):
        raise TypeError("Invalid write payload")
    return value


def _request_id(value: object) -> None:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Invalid write request ID")


@dataclass
class WriteItemRequest:
    tag: str
    value: Any
    data_type: str = "float"

    def to_dict(self) -> dict[str, Any]:
        return {"tag": self.tag, "value": self.value, "data_type": self.data_type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WriteItemRequest:
        if not isinstance(data, dict):
            raise TypeError("Invalid write item")
        tag = data.get("tag")
        if not valid_text(tag, 1024):
            raise ValueError(f"Invalid tag address: {tag}")
        data_type = data.get("data_type", "float")
        if data_type not in ALLOWED_DATA_TYPES:
            raise ValueError(f"Unsupported data type: {data_type}")
        return cls(tag=tag, value=data.get("value"), data_type=data_type)


@dataclass
class WriteItemResult:
    tag: str
    status: str  # "applied", "rejected", "error", "timeout"
    value: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "status": self.status,
            "value": self.value,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WriteItemResult:
        if not isinstance(data, dict):
            raise TypeError("Invalid write item result")
        tag = data.get("tag")
        if not valid_text(tag, 1024):
            raise ValueError("Invalid tag in write result")
        status = data.get("status", "error")
        if status not in {"applied", "rejected", "error", "timeout"}:
            status = "error"
        return cls(
            tag=tag,
            status=status,
            value=data.get("value"),
            error=data.get("error"),
        )


@dataclass
class WriteRequest:
    request_id: str
    opc_prog_id: str
    items: list[WriteItemRequest]
    user: str = "admin"

    def pack(self) -> bytes:
        payload = {
            "request_id": self.request_id,
            "opc_prog_id": self.opc_prog_id,
            "items": [item.to_dict() for item in self.items],
            "user": self.user,
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.unpack(raw)
        return raw

    @classmethod
    def unpack(cls, raw: bytes) -> WriteRequest:
        data = _decode(raw)
        if set(data) - {"request_id", "opc_prog_id", "items", "user"}:
            raise ValueError("Invalid write request fields")
        _request_id(data["request_id"])
        prog_id = data.get("opc_prog_id", "")
        if not valid_text(prog_id, 256):
            raise ValueError("Invalid opc_prog_id in write request")
        raw_items = data.get("items")
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_WRITE_ITEMS:
            raise ValueError("Invalid items in write request")
        items = [WriteItemRequest.from_dict(item) for item in raw_items]
        user = data.get("user", "admin")
        if not isinstance(user, str) or len(user) > 128:
            user = "admin"
        return cls(
            request_id=data["request_id"],
            opc_prog_id=prog_id,
            items=items,
            user=user,
        )


@dataclass
class WriteResponse:
    request_id: str
    error: str | None = None
    results: list[WriteItemResult] | None = None

    def pack(self) -> bytes:
        payload: dict[str, Any] = {
            "request_id": self.request_id,
            "error": self.error,
            "results": [r.to_dict() for r in self.results] if self.results is not None else None,
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.unpack(raw)
        return raw

    @classmethod
    def unpack(cls, raw: bytes) -> WriteResponse:
        data = _decode(raw)
        if set(data) != {"request_id", "error", "results"}:
            raise ValueError("Invalid write response fields")
        _request_id(data["request_id"])
        err = data.get("error")
        if err is not None and not isinstance(err, str):
            raise ValueError("Invalid error field in write response")
        results = None
        if data.get("results") is not None:
            if not isinstance(data["results"], list) or len(data["results"]) > MAX_WRITE_ITEMS:
                raise ValueError("Invalid results list in write response")
            results = [WriteItemResult.from_dict(r) for r in data["results"]]
        return cls(request_id=data["request_id"], error=err, results=results)
