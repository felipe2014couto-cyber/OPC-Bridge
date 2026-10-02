"""Shared agent credential hashing and verification helpers."""
from __future__ import annotations

import hashlib
import hmac


def encode_agent_credential(token: str) -> str:
    """Return the persisted SHA-256 representation used by BridgeServer."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_agent_credential_hash(received_hash: bytes, stored_hash: str) -> bool:
    """Compare a protocol SHA-256 digest to a stored credential in constant time."""
    encoded_hash = stored_hash.split(":", 1)[-1]
    try:
        expected_hash = bytes.fromhex(encoded_hash)
    except ValueError:
        expected_hash = b""
    return hmac.compare_digest(received_hash, expected_hash)
