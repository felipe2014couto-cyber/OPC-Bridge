"""Robustness tests for OPC-Bridge protocol: fragmentation, concatenation, limits and malformed input."""
from __future__ import annotations

import struct

import pytest

from opc_bridge.protocol import (
    HEADER_SIZE,
    TRAILER_SIZE,
    HelloPayload,
    ItemRef,
    MsgType,
    ReadRequestPayload,
    frame_message,
    unframe_message,
)


class TestFragmentation:
    """Simulate TCP splitting a single frame into multiple recv() calls."""

    def test_split_in_header(self):
        framed = frame_message(MsgType.HEARTBEAT, seq=1, payload=b"\xaa\xbb")
        # Reassemble from two fragments split inside the header
        part1 = framed[:6]
        part2 = framed[6:]
        reassembled = part1 + part2
        header, payload = unframe_message(reassembled)
        assert header.msg_type == MsgType.HEARTBEAT
        assert payload == b"\xaa\xbb"

    def test_split_between_header_and_payload(self):
        payload = b"X" * 32
        framed = frame_message(MsgType.READ_REQUEST, seq=5, payload=payload)
        split_at = HEADER_SIZE
        reassembled = framed[:split_at] + framed[split_at:]
        header, body = unframe_message(reassembled)
        assert header.payload_len == 32
        assert body == payload

    def test_split_in_trailer(self):
        payload = b"Y" * 16
        framed = frame_message(MsgType.ERROR, seq=3, payload=payload)
        # Split one byte before end (inside CRC trailer)
        split_at = len(framed) - 1
        reassembled = framed[:split_at] + framed[split_at:]
        _header, body = unframe_message(reassembled)
        assert body == payload

    def test_byte_by_byte_reassembly(self):
        payload = b"Z" * 8
        framed = frame_message(MsgType.HEARTBEAT, seq=0, payload=payload)
        # Simulate worst-case: one byte at a time
        buf = bytearray()
        for b in framed:
            buf.append(b)
        _header, body = unframe_message(bytes(buf))
        assert body == payload


class TestConcatenation:
    """Multiple frames arriving in a single recv() buffer."""

    def test_two_frames_concatenated(self):
        f1 = frame_message(MsgType.HEARTBEAT, seq=1, payload=b"\x01")
        f2 = frame_message(MsgType.HEARTBEAT, seq=2, payload=b"\x02")
        combined = f1 + f2
        # Parse first frame
        h1, p1 = unframe_message(combined)
        assert h1.seq == 1
        assert p1 == b"\x01"
        # Parse second frame from remainder
        remainder = combined[HEADER_SIZE + h1.payload_len + TRAILER_SIZE:]
        h2, p2 = unframe_message(remainder)
        assert h2.seq == 2
        assert p2 == b"\x02"

    def test_three_frames_concatenated(self):
        frames = []
        for i in range(3):
            frames.append(frame_message(MsgType.HEARTBEAT, seq=i, payload=bytes([i])))
        combined = b"".join(frames)
        offset = 0
        for expected_seq in range(3):
            chunk = combined[offset:]
            h, p = unframe_message(chunk)
            assert h.seq == expected_seq
            assert p == bytes([expected_seq])
            offset += HEADER_SIZE + h.payload_len + TRAILER_SIZE

    def test_mixed_message_types_concatenated(self):
        hello = HelloPayload(agent_id="a", hostname="h", os_version="o", capabilities=[])
        f_hello = frame_message(MsgType.HELLO, seq=1, payload=hello.pack())
        f_hb = frame_message(MsgType.HEARTBEAT, seq=2, payload=struct.pack("<Q", 999))
        combined = f_hello + f_hb
        h1, p1 = unframe_message(combined)
        assert h1.msg_type == MsgType.HELLO
        parsed_hello = HelloPayload.unpack(p1)
        assert parsed_hello.agent_id == "a"
        remainder = combined[HEADER_SIZE + h1.payload_len + TRAILER_SIZE:]
        h2, _p2 = unframe_message(remainder)
        assert h2.msg_type == MsgType.HEARTBEAT


class TestSizeLimits:
    """Boundary conditions for payload sizes."""

    def test_max_u16_string_length(self):
        """String with max u16 length (65535 bytes) should round-trip."""
        long_str = "A" * 65535
        item = ItemRef(item_id=1, opc_item_path=long_str, requested_source=0)
        packed = item.pack()
        unpacked, off = ItemRef.unpack(packed)
        assert unpacked.opc_item_path == long_str
        assert off == len(packed)

    def test_large_batch_items(self):
        """ReadRequest with 1000 items should round-trip correctly."""
        items = [ItemRef(item_id=i, opc_item_path=f"Tag.{i}", requested_source=0) for i in range(1000)]
        req = ReadRequestPayload(request_id=42, items=items)
        packed = req.pack()
        unpacked = ReadRequestPayload.unpack(packed)
        assert unpacked.request_id == 42
        assert len(unpacked.items) == 1000
        assert unpacked.items[999].opc_item_path == "Tag.999"

    def test_empty_payload_frame(self):
        framed = frame_message(MsgType.HEARTBEAT, seq=0, payload=b"")
        header, body = unframe_message(framed)
        assert header.payload_len == 0
        assert body == b""

    def test_single_byte_payload(self):
        framed = frame_message(MsgType.HEARTBEAT, seq=0, payload=b"\xff")
        header, body = unframe_message(framed)
        assert header.payload_len == 1
        assert body == b"\xff"


class TestMalformedInput:
    """Invalid/corrupted messages must raise ValueError with descriptive messages."""

    def test_zero_magic(self):
        data = b"\x00" * (HEADER_SIZE + TRAILER_SIZE)
        with pytest.raises(ValueError, match="too short|Invalid magic"):
            unframe_message(data)

    def test_wrong_magic_value(self):
        framed = bytearray(frame_message(MsgType.HEARTBEAT, seq=0, payload=b"x"))
        framed[0] = 0xFF
        framed[1] = 0xFF
        with pytest.raises(ValueError, match="Invalid magic"):
            unframe_message(bytes(framed))

    def test_wrong_version(self):
        framed = bytearray(frame_message(MsgType.HEARTBEAT, seq=0, payload=b"x"))
        framed[2] = 0xFF
        framed[3] = 0xFF
        with pytest.raises(ValueError, match="Unsupported version"):
            unframe_message(bytes(framed))

    def test_corrupted_crc(self):
        framed = bytearray(frame_message(MsgType.HEARTBEAT, seq=0, payload=b"test"))
        framed[-1] ^= 0xFF
        with pytest.raises(ValueError, match="CRC mismatch"):
            unframe_message(bytes(framed))

    def test_payload_len_exceeds_data(self):
        framed = bytearray(frame_message(MsgType.HEARTBEAT, seq=0, payload=b"abc"))
        # Tamper payload_len field to claim 1000 bytes
        struct.pack_into("<I", framed, 12, 1000)
        with pytest.raises(ValueError, match="Incomplete message"):
            unframe_message(bytes(framed))

    def test_zero_length_but_trailer_present(self):
        """Header claims 0 payload but data has extra bytes — should still parse."""
        framed = frame_message(MsgType.HEARTBEAT, seq=0, payload=b"")
        header, body = unframe_message(framed)
        assert header.payload_len == 0
        assert body == b""

    def test_completely_random_bytes(self):
        import os
        random_data = os.urandom(HEADER_SIZE + TRAILER_SIZE + 10)
        with pytest.raises(ValueError):
            unframe_message(random_data)

    def test_header_only_no_trailer(self):
        """Exactly HEADER_SIZE bytes — too short for trailer."""
        data = b"\x50\x4F\x01\x00\x09\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        with pytest.raises(ValueError, match="too short"):
            unframe_message(data[:HEADER_SIZE])


class TestSequenceNumbers:
    """Verify sequence tracking across multiple messages."""

    def test_incrementing_sequences(self):
        for seq in range(10):
            framed = frame_message(MsgType.HEARTBEAT, seq=seq, payload=b"\x00")
            header, _ = unframe_message(framed)
            assert header.seq == seq

    def test_max_sequence_u32(self):
        max_seq = 0xFFFFFFFF
        framed = frame_message(MsgType.HEARTBEAT, seq=max_seq, payload=b"\x00")
        header, _ = unframe_message(framed)
        assert header.seq == max_seq

    def test_sequence_zero(self):
        framed = frame_message(MsgType.HEARTBEAT, seq=0, payload=b"\x00")
        header, _ = unframe_message(framed)
        assert header.seq == 0