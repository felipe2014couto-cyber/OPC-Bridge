"""Small SQL repositories for initial control-plane persistence."""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Type, TypeVar

from .models import (
    Agent,
    AgentCredential,
    AgentSession,
    AuditEvent,
    CollectionPlan,
    ConfigOperation,
    ConfigSnapshot,
)

T = TypeVar("T")


class PersistenceRepository:
    """Repository bound to the transaction-scoped connection in Database.session()."""

    def __init__(self, connection: Any, dialect: str) -> None:
        self.connection = connection
        self.dialect = dialect
        self.placeholder = "%s" if dialect == "postgresql" else "?"

    def _execute(self, query: str, parameters: tuple = ()) -> Any:
        cursor = self.connection.cursor()
        cursor.execute(query.replace("?", self.placeholder), parameters)
        return cursor

    def _one(self, cursor: Any, model: Type[T]) -> Optional[T]:
        row = cursor.fetchone()
        return self._model(row, cursor, model) if row is not None else None

    def _model(self, row: Any, cursor: Any, model: Type[T]) -> T:
        if hasattr(row, "keys"):
            values = dict(row)
        else:
            values = {description[0]: value for description, value in zip(cursor.description, row)}
        values = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in values.items()
        }
        if model in (Agent, CollectionPlan) and "enabled" in values:
            values["enabled"] = bool(values["enabled"])
        return model(**values)

    def _list(self, cursor: Any, model: Type[T]) -> List[T]:
        return [self._model(row, cursor, model) for row in cursor.fetchall()]

    def add_agent(self, agent_id: str, display_name: str) -> Agent:
        self._execute("INSERT INTO agents(agent_id, display_name) VALUES (?, ?)", (agent_id, display_name))
        return Agent(agent_id=agent_id, display_name=display_name)

    def add_credential(self, agent_id: str, credential_id: str, credential_hash: str) -> AgentCredential:
        if not credential_hash:
            raise ValueError("credential_hash must be non-empty")
        self._execute(
            "INSERT INTO agent_credentials(credential_id, agent_id, credential_hash) VALUES (?, ?, ?)",
            (credential_id, agent_id, credential_hash),
        )
        return AgentCredential(credential_id, agent_id, credential_hash)

    def add_session(
        self,
        agent_id: str,
        session_id: str,
        hostname: str = "",
        os_version: str = "",
        applied_config_version: Optional[int] = None,
    ) -> AgentSession:
        self._execute(
            "INSERT INTO agent_sessions(session_id, agent_id, hostname, os_version, "
            "applied_config_version) VALUES (?, ?, ?, ?, ?)",
            (session_id, agent_id, hostname, os_version, applied_config_version),
        )
        return AgentSession(
            session_id,
            agent_id,
            hostname=hostname,
            os_version=os_version,
            applied_config_version=applied_config_version,
        )

    def active_credential_hashes(self, agent_id: str) -> List[str]:
        cursor = self._execute(
            "SELECT credential_hash FROM agent_credentials "
            "WHERE agent_id = ? AND revoked_at IS NULL ORDER BY created_at",
            (agent_id,),
        )
        return [row[0] for row in cursor.fetchall()]

    def update_session_observed(
        self, session_id: str, state: str, observed_state_json: str, heartbeat_at: Optional[str] = None
    ) -> None:
        self._execute(
            "UPDATE agent_sessions SET state = ?, observed_state_json = ?, "
            "last_heartbeat_at = COALESCE(?, last_heartbeat_at) WHERE session_id = ?",
            (state, observed_state_json, heartbeat_at, session_id),
        )

    def update_session_applied_version(self, session_id: str, version: int) -> None:
        self._execute(
            "UPDATE agent_sessions SET applied_config_version = ?, observed_config_version = ? "
            "WHERE session_id = ?",
            (version, version, session_id),
        )

    def disconnect_session(self, session_id: str, observed_state_json: str) -> None:
        self._execute(
            "UPDATE agent_sessions SET state = 'disconnected', ended_at = CURRENT_TIMESTAMP, "
            "observed_state_json = ? WHERE session_id = ? AND ended_at IS NULL",
            (observed_state_json, session_id),
        )

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        return self._one(
            self._execute(
                "SELECT session_id, agent_id, started_at, ended_at, applied_config_version, "
                "observed_config_version, hostname, os_version, state, last_heartbeat_at, "
                "observed_state_json FROM agent_sessions WHERE session_id = ?",
                (session_id,),
            ),
            AgentSession,
        )

    def recover_interrupted_sessions(self) -> None:
        self._execute(
            "UPDATE agent_sessions SET state = 'disconnected', ended_at = CURRENT_TIMESTAMP "
            "WHERE state = 'connected' AND ended_at IS NULL"
        )

    def complete_operation(self, operation_id: str, status: str) -> None:
        if status not in {"applied", "rejected", "failed", "expired"}:
            raise ValueError("invalid terminal config operation status")
        self._execute(
            "UPDATE config_operations SET status = ?, completed_at = CURRENT_TIMESTAMP "
            "WHERE operation_id = ? AND status = 'pending'",
            (status, operation_id),
        )

    def pending_operations(self) -> List[ConfigOperation]:
        return self._list(
            self._execute(
                "SELECT operation_id, agent_id, snapshot_id, plan_id, status, requested_at, completed_at "
                "FROM config_operations WHERE status = 'pending' ORDER BY requested_at"
            ),
            ConfigOperation,
        )

    def add_snapshot_if_absent(
        self, agent_id: str, snapshot_id: str, version: int, payload_json: str
    ) -> ConfigSnapshot:
        existing = self.get_snapshot_by_version(agent_id, version)
        if existing is not None:
            if existing.payload_json != payload_json:
                raise ValueError("configuration version already has a different snapshot")
            return existing
        return self.add_snapshot(agent_id, snapshot_id, version, payload_json)

    def get_snapshot_by_version(self, agent_id: str, version: int) -> Optional[ConfigSnapshot]:
        return self._one(
            self._execute(
                "SELECT snapshot_id, agent_id, version, payload_json, created_at "
                "FROM config_snapshots WHERE agent_id = ? AND version = ?",
                (agent_id, version),
            ),
            ConfigSnapshot,
        )

    def latest_snapshot(self) -> Optional[ConfigSnapshot]:
        return self._one(
            self._execute(
                "SELECT snapshot_id, agent_id, version, payload_json, created_at "
                "FROM config_snapshots ORDER BY created_at DESC, snapshot_id DESC LIMIT 1"
            ),
            ConfigSnapshot,
        )

    def latest_applied_snapshot(self, agent_id: str) -> Optional[ConfigSnapshot]:
        return self._one(
            self._execute(
                "SELECT s.snapshot_id, s.agent_id, s.version, s.payload_json, s.created_at "
                "FROM config_snapshots s JOIN config_operations o "
                "ON o.snapshot_id = s.snapshot_id AND o.agent_id = s.agent_id "
                "WHERE s.agent_id = ? AND o.status = 'applied' "
                "ORDER BY o.completed_at DESC, s.version DESC LIMIT 1",
                (agent_id,),
            ),
            ConfigSnapshot,
        )

    def add_collection_plan(self, agent_id: str, plan_id: str, interval_ms: int) -> CollectionPlan:
        if interval_ms <= 0:
            raise ValueError("interval_ms must be positive")
        self._execute(
            "INSERT INTO collection_plans(plan_id, agent_id, interval_ms) VALUES (?, ?, ?)",
            (plan_id, agent_id, interval_ms),
        )
        return CollectionPlan(plan_id, agent_id, interval_ms)

    def add_snapshot(
        self, agent_id: str, snapshot_id: str, version: int, payload_json: str
    ) -> ConfigSnapshot:
        if version < 0:
            raise ValueError("version must be non-negative")
        self._execute(
            "INSERT INTO config_snapshots(snapshot_id, agent_id, version, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (snapshot_id, agent_id, version, payload_json),
        )
        return ConfigSnapshot(snapshot_id, agent_id, version, payload_json)

    def add_operation(
        self,
        agent_id: str,
        operation_id: str,
        snapshot_id: str,
        plan_id: Optional[str] = None,
    ) -> ConfigOperation:
        self._execute(
            "INSERT INTO config_operations(operation_id, agent_id, snapshot_id, plan_id) "
            "VALUES (?, ?, ?, ?)",
            (operation_id, agent_id, snapshot_id, plan_id),
        )
        return ConfigOperation(operation_id, agent_id, snapshot_id, plan_id)

    def add_audit_event(
        self, agent_id: str, event_id: str, event_type: str, detail_json: str = "{}"
    ) -> AuditEvent:
        self._execute(
            "INSERT INTO audit_events(event_id, agent_id, event_type, detail_json) VALUES (?, ?, ?, ?)",
            (event_id, agent_id, event_type, detail_json),
        )
        return AuditEvent(event_id, agent_id, event_type, detail_json)

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        return self._one(
            self._execute("SELECT agent_id, display_name, created_at, enabled FROM agents WHERE agent_id = ?", (agent_id,)),
            Agent,
        )

    def get_credential(self, credential_id: str) -> Optional[AgentCredential]:
        return self._one(
            self._execute(
                "SELECT credential_id, agent_id, credential_hash, created_at, revoked_at "
                "FROM agent_credentials WHERE credential_id = ?",
                (credential_id,),
            ),
            AgentCredential,
        )

    def list_snapshots(self, agent_id: str) -> List[ConfigSnapshot]:
        return self._list(
            self._execute(
                "SELECT snapshot_id, agent_id, version, payload_json, created_at "
                "FROM config_snapshots WHERE agent_id = ? ORDER BY version",
                (agent_id,),
            ),
            ConfigSnapshot,
        )

    def list_operations(self, agent_id: str) -> List[ConfigOperation]:
        return self._list(
            self._execute(
                "SELECT operation_id, agent_id, snapshot_id, plan_id, status, requested_at, completed_at "
                "FROM config_operations WHERE agent_id = ? ORDER BY requested_at",
                (agent_id,),
            ),
            ConfigOperation,
        )
