"""Round-trip tests for OPC-Bridge protocol messages."""
from __future__ import annotations

import struct

import pytest

from opc_bridge.protocol import (
    MAGIC,
    VERSION,
    HEADER_SIZE,
    TRAILER_SIZE,
    MsgType,
    ItemStatus,
    ValueType,
    Header,
    frame_message,
    unframe_message,
    HelloPayload,
    HelloAckPayload,
    AuthPayload,
    AuthAckPayload,
    ItemRef,
    ReadRequestPayload,
    ItemResult,
    ReadResponsePayload,
    HeartbeatPayload,
    ErrorPayload,
    ConfigPushPayload,
    ConfigAckPayload,
)


class TestFraming:
    def test_frame_unframe_roundtrip(self):
        payload = b"hello world"
        framed = frame_message(MsgType.HEARTBEAT, seq=42, payload=payload)
        header, body = unframe_message(framed)
        assert header.magic == MAGIC
        assert header.version == VERSION
        assert header.msg_type == MsgType.HEARTBEAT
        assert header.seq == 42
        assert header.payload_len == len(payload)
        assert body == payload

    def test_frame_unframe_empty_payload(self):
        framed = frame_message(MsgType.ERROR, seq=0, payload=b"")
        header, body = unframe_message(framed)
        assert header.payload_len == 0
        assert body == b""

    def test_unframe_invalid_magic(self):
        framed = bytearray(frame_message(MsgType.HEARTBEAT, seq=0, payload=b"x"))
        # Corrupt magic bytes
        framed[0] = 0xFF
        with pytest.raises(ValueError, match="Invalid magic"):
            unframe_message(bytes(framed))

    def test_unframe_invalid_version(self):
        framed = bytearray(frame_message(MsgType.HEARTBEAT, seq=0, payload=b"x"))
        # Corrupt version bytes
        framed[2] = 0xFF
        with pytest.raises(ValueError, match="Unsupported version"):
            unframe_message(bytes(framed))

    def test_unframe_crc_mismatch(self):
        framed = bytearray(frame_message(MsgType.HEARTBEAT, seq=0, payload=b"x"))
        # Corrupt last byte of CRC trailer
        framed[-1] ^= 0xFF
        with pytest.raises(ValueError, match="CRC mismatch"):
            unframe_message(bytes(framed))

    def test_unframe_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            unframe_message(b"\x00" * (HEADER_SIZE + TRAILER_SIZE - 1))

    def test_unframe_incomplete_payload(self):
        # Payload must be large enough that truncation still exceeds HEADER_SIZE + TRAILER_SIZE
        payload = b"A" * 64
        framed = frame_message(MsgType.HEARTBEAT, seq=0, payload=payload)
        # Include full header + partial payload, but omit trailer and remaining payload
        truncated = framed[: HEADER_SIZE + 10]
        with pytest.raises(ValueError, match="Incomplete message"):
            unframe_message(truncated)


class TestHelloPayload:
    def test_roundtrip(self):
        p = HelloPayload(
            agent_id="agent-001",
            hostname="win-pc-01",
            os_version="Windows 10 22H2",
            capabilities=["opc-da", "tls"],
        )
        packed = p.pack()
        unpacked = HelloPayload.unpack(packed)
        assert unpacked.agent_id == p.agent_id
        assert unpacked.hostname == p.hostname
        assert unpacked.os_version == p.os_version
        assert unpacked.capabilities == p.capabilities

    def test_empty_capabilities(self):
        p = HelloPayload(agent_id="a", hostname="h", os_version="o", capabilities=[])
        unpacked = HelloPayload.unpack(p.pack())
        assert unpacked.capabilities == []

    def test_framed_roundtrip(self):
        p = HelloPayload(
            agent_id="agent-002",
            hostname="host",
            os_version="Win7",
            capabilities=["opc-da"],
        )
        framed = frame_message(MsgType.HELLO, seq=1, payload=p.pack())
        header, body = unframe_message(framed)
        assert header.msg_type == MsgType.HELLO
        unpacked = HelloPayload.unpack(body)
        assert unpacked.agent_id == "agent-002"


class TestHelloAckPayload:
    def test_roundtrip(self):
        p = HelloAckPayload(session_id="sess-abc-123", heartbeat_interval_ms=5000)
        unpacked = HelloAckPayload.unpack(p.pack())
        assert unpacked.session_id == p.session_id
        assert unpacked.heartbeat_interval_ms == p.heartbeat_interval_ms


class TestAuthPayload:
    def test_roundtrip(self):
        token_hash = b"\x01\x02\x03\x04" * 8  # 32 bytes SHA-256
        p = AuthPayload(token_hash=token_hash)
        unpacked = AuthPayload.unpack(p.pack())
        assert unpacked.token_hash == token_hash

    def test_empty_token_hash(self):
        p = AuthPayload(token_hash=b"")
        unpacked = AuthPayload.unpack(p.pack())
        assert unpacked.token_hash == b""


class TestAuthAckPayload:
    def test_success(self):
        p = AuthAckPayload(success=True, policies_version=3)
        unpacked = AuthAckPayload.unpack(p.pack())
        assert unpacked.success is True
        assert unpacked.policies_version == 3

    def test_failure(self):
        p = AuthAckPayload(success=False, policies_version=0)
        unpacked = AuthAckPayload.unpack(p.pack())
        assert unpacked.success is False


class TestItemRef:
    def test_roundtrip(self):
        item = ItemRef(item_id=42, opc_item_path="Pims_A40:gUsw.ToPims.DIAMETRO_CALC_BOBIN", requested_source=0)
        packed = item.pack()
        unpacked, off = ItemRef.unpack(packed)
        assert unpacked.item_id == item.item_id
        assert unpacked.opc_item_path == item.opc_item_path
        assert unpacked.requested_source == item.requested_source
        assert off == len(packed)

    def test_unicode_path(self):
        item = ItemRef(item_id=1, opc_item_path="Tag_éèê", requested_source=0)
        unpacked, _ = ItemRef.unpack(item.pack())
        assert unpacked.opc_item_path == "Tag_éèê"


class TestReadRequestPayload:
    def test_roundtrip_multiple_items(self):
        items = [
            ItemRef(item_id=1, opc_item_path="Tag.A", requested_source=0),
            ItemRef(item_id=2, opc_item_path="Tag.B", requested_source=0),
            ItemRef(item_id=3, opc_item_path="Tag.C", requested_source=0),
        ]
        p = ReadRequestPayload(request_id=100, items=items)
        unpacked = ReadRequestPayload.unpack(p.pack())
        assert unpacked.request_id == 100
        assert len(unpacked.items) == 3
        for orig, parsed in zip(items, unpacked.items):
            assert parsed.item_id == orig.item_id
            assert parsed.opc_item_path == orig.opc_item_path

    def test_empty_items(self):
        p = ReadRequestPayload(request_id=0, items=[])
        unpacked = ReadRequestPayload.unpack(p.pack())
        assert unpacked.items == []

    def test_framed_roundtrip(self):
        items = [ItemRef(item_id=10, opc_item_path="X", requested_source=0)]
        p = ReadRequestPayload(request_id=55, items=items)
        framed = frame_message(MsgType.READ_REQUEST, seq=7, payload=p.pack())
        header, body = unframe_message(framed)
        assert header.msg_type == MsgType.READ_REQUEST
        unpacked = ReadRequestPayload.unpack(body)
        assert unpacked.request_id == 55
        assert len(unpacked.items) == 1


class TestItemResult:
    def test_roundtrip_ok(self):
        r = ItemResult(
            item_id=1,
            status=ItemStatus.OK,
            value_type=ValueType.F64,
            quality=192,
            timestamp_us=1700000000000000,
            value=struct.pack("<d", 3.14159),
            error_code=0,
        )
        packed = r.pack()
        unpacked, off = ItemResult.unpack(packed)
        assert unpacked.item_id == r.item_id
        assert unpacked.status == r.status
        assert unpacked.value_type == r.value_type
        assert unpacked.quality == r.quality
        assert unpacked.timestamp_us == r.timestamp_us
        assert unpacked.value == r.value
        assert unpacked.error_code == 0
        assert off == len(packed)

    def test_roundtrip_error(self):
        r = ItemResult(
            item_id=99,
            status=ItemStatus.NOT_FOUND,
            value_type=ValueType.I32,
            quality=0,
            timestamp_us=0,
            value=b"",
            error_code=0x80040001,
        )
        unpacked, _ = ItemResult.unpack(r.pack())
        assert unpacked.status == ItemStatus.NOT_FOUND
        assert unpacked.error_code == 0x80040001

    def test_string_value(self):
        val = "running".encode("utf-8")
        r = ItemResult(
            item_id=5,
            status=ItemStatus.OK,
            value_type=ValueType.STRING,
            quality=255,
            timestamp_us=1234567890,
            value=val,
            error_code=0,
        )
        unpacked, _ = ItemResult.unpack(r.pack())
        assert unpacked.value == val


class TestReadResponsePayload:
    def test_roundtrip_multiple_results(self):
        results = [
            ItemResult(item_id=1, status=ItemStatus.OK, value_type=ValueType.F32,
                       quality=192, timestamp_us=1000, value=struct.pack("<f", 1.5), error_code=0),
            ItemResult(item_id=2, status=ItemStatus.BAD_QUALITY, value_type=ValueType.BOOL,
                       quality=0, timestamp_us=2000, value=struct.pack("<?", False), error_code=0),
        ]
        p = ReadResponsePayload(request_id=100, duration_us=500, results=results)
        unpacked = ReadResponsePayload.unpack(p.pack())
        assert unpacked.request_id == 100
        assert unpacked.duration_us == 500
        assert len(unpacked.results) == 2
        assert unpacked.results[0].item_id == 1
        assert unpacked.results[1].status == ItemStatus.BAD_QUALITY

    def test_framed_roundtrip(self):
        results = [
            ItemResult(item_id=1, status=ItemStatus.OK, value_type=ValueType.I16,
                       quality=192, timestamp_us=999, value=struct.pack("<h", -42), error_code=0),
        ]
        p = ReadResponsePayload(request_id=77, duration_us=1234, results=results)
        framed = frame_message(MsgType.READ_RESPONSE, seq=8, payload=p.pack())
        header, body = unframe_message(framed)
        assert header.msg_type == MsgType.READ_RESPONSE
        unpacked = ReadResponsePayload.unpack(body)
        assert unpacked.request_id == 77
        assert unpacked.duration_us == 1234
        assert len(unpacked.results) == 1


class TestHeartbeatPayload:
    def test_roundtrip(self):
        p = HeartbeatPayload(timestamp_us=1700000000000000)
        unpacked = HeartbeatPayload.unpack(p.pack())
        assert unpacked.timestamp_us == p.timestamp_us


class TestErrorPayload:
    def test_roundtrip(self):
        p = ErrorPayload(code=4001, message="Connection lost to OPC server")
        unpacked = ErrorPayload.unpack(p.pack())
        assert unpacked.code == 4001
        assert unpacked.message == "Connection lost to OPC server"

    def test_empty_message(self):
        p = ErrorPayload(code=0, message="")
        unpacked = ErrorPayload.unpack(p.pack())
        assert unpacked.message == ""


class TestConfigPushPayload:
    def test_roundtrip(self):
        items = [
            ItemRef(item_id=1, opc_item_path="Tag.A", requested_source=0),
            ItemRef(item_id=2, opc_item_path="Tag.B", requested_source=0),
        ]
        p = ConfigPushPayload(config_version=5, update_rate_ms=1000, items=items)
        unpacked = ConfigPushPayload.unpack(p.pack())
        assert unpacked.config_version == 5
        assert unpacked.update_rate_ms == 1000
        assert len(unpacked.items) == 2
        assert unpacked.items[0].opc_item_path == "Tag.A"

    def test_empty_items(self):
        p = ConfigPushPayload(config_version=1, update_rate_ms=500, items=[])
        unpacked = ConfigPushPayload.unpack(p.pack())
        assert unpacked.items == []


class TestConfigAckPayload:
    def test_applied(self):
        p = ConfigAckPayload(config_version=5, applied=True)
        unpacked = ConfigAckPayload.unpack(p.pack())
        assert unpacked.config_version == 5
        assert unpacked.applied is True

    def test_rejected(self):
        p = ConfigAckPayload(config_version=3, applied=False)
        unpacked = ConfigAckPayload.unpack(p.pack())
        assert unpacked.applied is False


class TestEndToEndMessageTypes:
    """Verify every message type can be framed and unframed correctly."""

    @pytest.mark.parametrize("msg_type,payload_cls,payload_args", [
        (MsgType.HELLO, HelloPayload, {"agent_id": "a", "hostname": "h", "os_version": "o", "capabilities": ["c"]}),
        (MsgType.HELLO_ACK, HelloAckPayload, {"session_id": "s", "heartbeat_interval_ms": 1000}),
        (MsgType.AUTH, AuthPayload, {"token_hash": b"\xaa" * 32}),
        (MsgType.AUTH_ACK, AuthAckPayload, {"success": True, "policies_version": 1}),
        (MsgType.HEARTBEAT, HeartbeatPayload, {"timestamp_us": 999}),
        (MsgType.ERROR, ErrorPayload, {"code": 1, "message": "err"}),
        (MsgType.CONFIG_ACK, ConfigAckPayload, {"config_version": 2, "applied": True}),
    ])
    def test_all_message_types(self, msg_type, payload_cls, payload_args):
        payload = payload_cls(**payload_args)
        framed = frame_message(msg_type, seq=1, payload=payload.pack())
        header, body = unframe_message(framed)
        assert header.msg_type == msg_type
        unpacked = payload_cls.unpack(body)
        repacked = unpacked.pack()
        assert repacked == payload.pack()