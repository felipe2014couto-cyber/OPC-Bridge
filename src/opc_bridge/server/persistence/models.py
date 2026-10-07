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
    hostname: str = ""
    os_version: str = ""
    state: str = "connected"
    last_heartbeat_at: Optional[str] = None
    observed_state_json: str = "{}"


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


@dataclass(frozen=True)
class Equipment:
    equipment_id: str
    name: str
    ip_address: str
    agent_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class NamedOpcConfig:
    config_id: str
    name: str
    equipment_id: str
    opc_prog_id: str
    interval_ms: int
    tags_json: str
    agent_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class PiMapping:
    mapping_id: str
    equipment_id: str
    opc_config_id: str
    opc_item_path: str
    item_id: int
    pi_point_name: str
    point_source: str
    location1: int
    publish_interval_ms: int
    enabled: bool = True
    last_publish_status: str = "Não configurado"
    last_published_at: Optional[str] = None
    last_published_value: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
