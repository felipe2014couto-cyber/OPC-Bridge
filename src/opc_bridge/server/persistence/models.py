"""Immutable row types for the initial OPC-Bridge control-plane store."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Agent:
    agent_id: str
    display_name: str
    created_at: Optional[str] = None
    enabled: bool = True


@dataclass(frozen=True)
class AgentCredential:
    credential_id: str
    agent_id: str
    credential_hash: str
    created_at: Optional[str] = None
    revoked_at: Optional[str] = None


@dataclass(frozen=True)
class AgentSession:
    session_id: str
    agent_id: str
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    applied_config_version: Optional[int] = None
    observed_config_version: Optional[int] = None


@dataclass(frozen=True)
class CollectionPlan:
    plan_id: str
    agent_id: str
    interval_ms: int
    enabled: bool = True
    created_at: Optional[str] = None


@dataclass(frozen=True)
class ConfigSnapshot:
    snapshot_id: str
    agent_id: str
    version: int
    payload_json: str
    created_at: Optional[str] = None


@dataclass(frozen=True)
class ConfigOperation:
    operation_id: str
    agent_id: str
    snapshot_id: str
    plan_id: Optional[str] = None
    status: str = "pending"
    requested_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    agent_id: str
    event_type: str
    detail_json: str = "{}"
    occurred_at: Optional[str] = None
