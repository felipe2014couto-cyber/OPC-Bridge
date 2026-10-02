"""Bounded, primitive-only inspection messages; existing framing is unchanged."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass

CAPABILITY = "opc-inspection-v1"
MAX_TAGS = 50
ERRORS = {None, "unavailable", "busy", "timeout", "inspection_failed", "invalid_request"}


def valid_text(value: object, limit: int = 1024) -> bool:
    if not isinstance(value, str):
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        return False
    return (bool(value) and value == value.strip() and size <= limit
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _decode(raw: bytes) -> dict:
    if len(raw) > 131072:
        raise ValueError("Inspection payload too large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate inspection field")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except RecursionError:
        raise ValueError("Invalid inspection nesting") from None
    if not isinstance(value, dict):
        raise TypeError("Invalid inspection payload")
    return value


def _request_id(value: object) -> None:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Invalid inspection ID")


@dataclass
class InspectionRequest:
    request_id: str
    action: str
    opc_prog_id: str = ""
    tags: list[str] | None = None

    def pack(self) -> bytes:
        raw = json.dumps(asdict(self), ensure_ascii=False).encode("utf-8")
        self.unpack(raw)
        return raw

    @classmethod
    def unpack(cls, raw: bytes) -> InspectionRequest:
        data = _decode(raw)
        if set(data) != {"request_id", "action", "opc_prog_id", "tags"}:
            raise ValueError("Invalid inspection fields")
        _request_id(data["request_id"])
        if data["action"] == "tags":
            tags = data["tags"]
            if (not valid_text(data["opc_prog_id"], 256) or not isinstance(tags, list)
                    or not 1 <= len(tags) <= MAX_TAGS or any(not valid_text(p) for p in tags)
                    or len(set(tags)) != len(tags)):
                raise ValueError("Invalid inspection tags")
        elif data["action"] != "servers" or data["opc_prog_id"] != "" or data["tags"] is not None:
            raise ValueError("Invalid inspection action")
        return cls(**data)


@dataclass
class InspectionResponse:
    request_id: str
    results: list[dict] | None = None
    servers: list[str] | None = None
    error: str | None = None

    def pack(self) -> bytes:
        raw = json.dumps(asdict(self), ensure_ascii=False).encode("utf-8")
        self.unpack(raw)
        return raw

    @classmethod
    def unpack(cls, raw: bytes) -> InspectionResponse:
        data = _decode(raw)
        if set(data) != {"request_id", "results", "servers", "error"}:
            raise ValueError("Invalid inspection response fields")
        _request_id(data["request_id"])
        if not isinstance(data["error"], (str, type(None))) or data["error"] not in ERRORS:
            raise ValueError("Invalid inspection error")
        if data["results"] is not None:
            results = data["results"]
            if not isinstance(results, list) or len(results) > MAX_TAGS:
                raise ValueError("Invalid inspection results")
            for result in results:
                if (not isinstance(result, dict) or set(result) != {"opc_item_path", "status", "hresult"}
                        or not valid_text(result["opc_item_path"])
                        or result["status"] not in ("valid", "invalid", "error")
                        or (result["hresult"] is not None and
                            (type(result["hresult"]) is not int or
                             not 0 <= result["hresult"] <= 0xFFFFFFFF))):
                    raise ValueError("Invalid inspection result")
        if data["servers"] is not None:
            servers = data["servers"]
            if (not isinstance(servers, list) or len(servers) > 100
                    or any(not valid_text(p, 256) for p in servers)):
                raise ValueError("Invalid OPC servers")
        return cls(**data)
