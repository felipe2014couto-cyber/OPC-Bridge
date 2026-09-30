"""OPC-Bridge protocol message definitions and framing.

Binary little-endian framing over TLS. See docs/contracts/protocol.md.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional


MAGIC = 0x4F50  # "OP"
VERSION = 0x0001
HEADER_FORMAT = "<HHHHII"  # magic, version, msg_type, flags, seq, payload_len
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
TRAILER_FORMAT = "<I"  # CRC32C placeholder (using CRC32 for now; swap to CRC32C in prod)
TRAILER_SIZE = struct.calcsize(TRAILER_FORMAT)


class MsgType(IntEnum):
    HELLO = 0x01
    HELLO_ACK = 0x02
    AUTH = 0x03
    AUTH_ACK = 0x04
    CONFIG_PUSH = 0x05
    CONFIG_ACK = 0x06
    READ_REQUEST = 0x07
    READ_RESPONSE = 0x08
    HEARTBEAT = 0x09
    ERROR = 0x0A


class ItemStatus(IntEnum):
    OK = 0
    BAD_QUALITY = 1
    NOT_FOUND = 2
    TIMEOUT = 3
    ERROR = 4


class ValueType(IntEnum):
    I16 = 0
    I32 = 1
    F32 = 2
    F64 = 3
    BOOL = 4
    STRING = 5
    BLOB = 6


@dataclass
class Header:
    magic: int = MAGIC
    version: int = VERSION
    msg_type: int = 0
    flags: int = 0
    seq: int = 0
    payload_len: int = 0

    def pack(self) -> bytes:
        return struct.pack(
            HEADER_FORMAT,
            self.magic,
            self.version,
            self.msg_type,
            self.flags,
            self.seq,
            self.payload_len,
        )

    @classmethod
    def unpack(cls, data: bytes) -> Header:
        if len(data) != HEADER_SIZE:
            raise ValueError(f"Header must be {HEADER_SIZE} bytes, got {len(data)}")
        magic, version, msg_type, flags, seq, payload_len = struct.unpack(HEADER_FORMAT, data)
        if magic != MAGIC:
            raise ValueError(f"Invalid magic: 0x{magic:04X}")
        if version != VERSION:
            raise ValueError(f"Unsupported version: 0x{version:04X}")
        return cls(magic=magic, version=version, msg_type=msg_type, flags=flags, seq=seq, payload_len=payload_len)


def _crc32c(data: bytes) -> int:
    """CRC32 checksum (placeholder for CRC32C). Swap to crcmod or hardware CRC32C in production."""
    return zlib.crc32(data) & 0xFFFFFFFF


def frame_message(msg_type: MsgType, seq: int, payload: bytes, flags: int = 0) -> bytes:
    """Build a complete framed message: header + payload + trailer."""
    header = Header(msg_type=int(msg_type), flags=flags, seq=seq, payload_len=len(payload))
    trailer = struct.pack(TRAILER_FORMAT, _crc32c(header.pack() + payload))
    return header.pack() + payload + trailer


def unframe_message(data: bytes) -> tuple[Header, bytes]:
    """Parse a framed message. Raises on invalid magic, version, length or checksum."""
    if len(data) < HEADER_SIZE + TRAILER_SIZE:
        raise ValueError("Message too short")
    header = Header.unpack(data[:HEADER_SIZE])
    expected_total = HEADER_SIZE + header.payload_len + TRAILER_SIZE
    if len(data) < expected_total:
        raise ValueError(f"Incomplete message: expected {expected_total}, got {len(data)}")
    payload = data[HEADER_SIZE : HEADER_SIZE + header.payload_len]
    received_crc = struct.unpack(TRAILER_FORMAT, data[HEADER_SIZE + header.payload_len : expected_total])[0]
    computed_crc = _crc32c(data[: HEADER_SIZE + header.payload_len])
    if received_crc != computed_crc:
        raise ValueError(f"CRC mismatch: expected 0x{computed_crc:08X}, got 0x{received_crc:08X}")
    return header, payload


# --- Payload builders/parsers ---

def encode_utf8(s: str) -> bytes:
    encoded = s.encode("utf-8")
    if len(encoded) > 65535:
        raise ValueError("String too long for u16 length prefix")
    return struct.pack("<H", len(encoded)) + encoded


def decode_utf8(data: bytes, offset: int = 0) -> tuple[str, int]:
    if offset + 2 > len(data):
        raise ValueError("Not enough data for string length")
    slen = struct.unpack_from("<H", data, offset)[0]
    start = offset + 2
    end = start + slen
    if end > len(data):
        raise ValueError("String extends beyond buffer")
    return data[start:end].decode("utf-8"), end


@dataclass
class HelloPayload:
    agent_id: str
    hostname: str
    os_version: str
    capabilities: list[str]

    def pack(self) -> bytes:
        buf = bytearray()
        buf.extend(encode_utf8(self.agent_id))
        buf.extend(encode_utf8(self.hostname))
        buf.extend(encode_utf8(self.os_version))
        buf.extend(struct.pack("<H", len(self.capabilities)))
        for cap in self.capabilities:
            buf.extend(encode_utf8(cap))
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes) -> HelloPayload:
        agent_id, off = decode_utf8(data, 0)
        hostname, off = decode_utf8(data, off)
        os_version, off = decode_utf8(data, off)
        caps_count = struct.unpack_from("<H", data, off)[0]
        off += 2
        capabilities = []
        for _ in range(caps_count):
            cap, off = decode_utf8(data, off)
            capabilities.append(cap)
        return cls(agent_id=agent_id, hostname=hostname, os_version=os_version, capabilities=capabilities)


@dataclass
class HelloAckPayload:
    session_id: str
    heartbeat_interval_ms: int

    def pack(self) -> bytes:
        buf = bytearray()
        buf.extend(encode_utf8(self.session_id))
        buf.extend(struct.pack("<I", self.heartbeat_interval_ms))
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes) -> HelloAckPayload:
        session_id, off = decode_utf8(data, 0)
        heartbeat_interval_ms = struct.unpack_from("<I", data, off)[0]
        return cls(session_id=session_id, heartbeat_interval_ms=heartbeat_interval_ms)


@dataclass
class AuthPayload:
    token_hash: bytes  # SHA-256 hash of auth token

    def pack(self) -> bytes:
        return struct.pack("<H", len(self.token_hash)) + self.token_hash

    @classmethod
    def unpack(cls, data: bytes) -> AuthPayload:
        tlen = struct.unpack_from("<H", data, 0)[0]
        token_hash = data[2 : 2 + tlen]
        return cls(token_hash=token_hash)


@dataclass
class AuthAckPayload:
    success: bool
    policies_version: int

    def pack(self) -> bytes:
        return struct.pack("<?I", self.success, self.policies_version)

    @classmethod
    def unpack(cls, data: bytes) -> AuthAckPayload:
        success, policies_version = struct.unpack_from("<?I", data, 0)
        return cls(success=success, policies_version=policies_version)


@dataclass
class ItemRef:
    item_id: int
    opc_item_path: str
    requested_source: int = 0  # 0=Device

    def pack(self) -> bytes:
        buf = bytearray()
        buf.extend(struct.pack("<I", self.item_id))
        buf.extend(encode_utf8(self.opc_item_path))
        buf.extend(struct.pack("<B", self.requested_source))
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> tuple[ItemRef, int]:
        item_id = struct.unpack_from("<I", data, offset)[0]
        opc_item_path, off = decode_utf8(data, offset + 4)
        requested_source = struct.unpack_from("<B", data, off)[0]
        return cls(item_id=item_id, opc_item_path=opc_item_path, requested_source=requested_source), off + 1


@dataclass
class ReadRequestPayload:
    request_id: int
    items: list[ItemRef]

    def pack(self) -> bytes:
        buf = bytearray()
        buf.extend(struct.pack("<I", self.request_id))
        buf.extend(struct.pack("<H", len(self.items)))
        for item in self.items:
            buf.extend(item.pack())
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes) -> ReadRequestPayload:
        request_id = struct.unpack_from("<I", data, 0)[0]
        count = struct.unpack_from("<H", data, 4)[0]
        off = 6
        items = []
        for _ in range(count):
            item, off = ItemRef.unpack(data, off)
            items.append(item)
        return cls(request_id=request_id, items=items)


@dataclass
class ItemResult:
    item_id: int
    status: int
    value_type: int
    quality: int
    timestamp_us: int
    value: bytes
    error_code: int = 0

    def pack(self) -> bytes:
        buf = bytearray()
        buf.extend(struct.pack("<I", self.item_id))
        buf.extend(struct.pack("<B", self.status))
        buf.extend(struct.pack("<B", self.value_type))
        buf.extend(struct.pack("<H", self.quality))
        buf.extend(struct.pack("<Q", self.timestamp_us))
        buf.extend(struct.pack("<I", len(self.value)))
        buf.extend(self.value)
        buf.extend(struct.pack("<I", self.error_code))
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> tuple[ItemResult, int]:
        item_id = struct.unpack_from("<I", data, offset)[0]
        status = struct.unpack_from("<B", data, offset + 4)[0]
        value_type = struct.unpack_from("<B", data, offset + 5)[0]
        quality = struct.unpack_from("<H", data, offset + 6)[0]
        timestamp_us = struct.unpack_from("<Q", data, offset + 8)[0]
        vlen = struct.unpack_from("<I", data, offset + 16)[0]
        value_start = offset + 20
        value = data[value_start : value_start + vlen]
        error_code = struct.unpack_from("<I", data, value_start + vlen)[0]
        return cls(
            item_id=item_id,
            status=status,
            value_type=value_type,
            quality=quality,
            timestamp_us=timestamp_us,
            value=value,
            error_code=error_code,
        ), value_start + vlen + 4


@dataclass
class ReadResponsePayload:
    request_id: int
    duration_us: int
    results: list[ItemResult]

    def pack(self) -> bytes:
        buf = bytearray()
        buf.extend(struct.pack("<I", self.request_id))
        buf.extend(struct.pack("<Q", self.duration_us))
        buf.extend(struct.pack("<H", len(self.results)))
        for result in self.results:
            buf.extend(result.pack())
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes) -> ReadResponsePayload:
        request_id = struct.unpack_from("<I", data, 0)[0]
        duration_us = struct.unpack_from("<Q", data, 4)[0]
        count = struct.unpack_from("<H", data, 12)[0]
        off = 14
        results = []
        for _ in range(count):
            result, off = ItemResult.unpack(data, off)
            results.append(result)
        return cls(request_id=request_id, duration_us=duration_us, results=results)


@dataclass
class HeartbeatPayload:
    timestamp_us: int

    def pack(self) -> bytes:
        return struct.pack("<Q", self.timestamp_us)

    @classmethod
    def unpack(cls, data: bytes) -> HeartbeatPayload:
        timestamp_us = struct.unpack_from("<Q", data, 0)[0]
        return cls(timestamp_us=timestamp_us)


@dataclass
class ErrorPayload:
    code: int
    message: str

    def pack(self) -> bytes:
        buf = bytearray()
        buf.extend(struct.pack("<I", self.code))
        buf.extend(encode_utf8(self.message))
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes) -> ErrorPayload:
        code = struct.unpack_from("<I", data, 0)[0]
        message, _ = decode_utf8(data, 4)
        return cls(code=code, message=message)


@dataclass
class ConfigPushPayload:
    config_version: int
    update_rate_ms: int
    items: list[ItemRef]

    def pack(self) -> bytes:
        buf = bytearray()
        buf.extend(struct.pack("<I", self.config_version))
        buf.extend(struct.pack("<I", self.update_rate_ms))
        buf.extend(struct.pack("<H", len(self.items)))
        for item in self.items:
            buf.extend(item.pack())
        return bytes(buf)

    @classmethod
    def unpack(cls, data: bytes) -> ConfigPushPayload:
        config_version = struct.unpack_from("<I", data, 0)[0]
        update_rate_ms = struct.unpack_from("<I", data, 4)[0]
        count = struct.unpack_from("<H", data, 8)[0]
        off = 10
        items = []
        for _ in range(count):
            item, off = ItemRef.unpack(data, off)
            items.append(item)
        return cls(config_version=config_version, update_rate_ms=update_rate_ms, items=items)


@dataclass
class ConfigAckPayload:
    config_version: int
    applied: bool

    def pack(self) -> bytes:
        return struct.pack("<I?", self.config_version, self.applied)

    @classmethod
    def unpack(cls, data: bytes) -> ConfigAckPayload:
        config_version, applied = struct.unpack_from("<I?", data, 0)
        return cls(config_version=config_version, applied=applied)