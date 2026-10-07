"""Small SQL repositories for initial control-plane persistence."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional, Type, TypeVar

from .models import (
    Agent,
    AgentCredential,
    AgentSession,
    AuditEvent,
    CollectionPlan,
    ConfigOperation,
    ConfigSnapshot,
    Equipment,
    NamedOpcConfig,
    PiMapping,
    PiProfile,
)


T = TypeVar("T")

_TERMINAL_CONFIG_STATUSES = "'applied', 'rejected', 'failed', 'expired'"
_LATEST_APPLIED_OPERATION_IDS = """
    SELECT active_op.operation_id
    FROM config_operations active_op
    JOIN config_snapshots active_snapshot
      ON active_snapshot.snapshot_id = active_op.snapshot_id
     AND active_snapshot.agent_id = active_op.agent_id
    WHERE active_op.status = 'applied' AND active_op.completed_at IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM config_operations newer_op
          JOIN config_snapshots newer_snapshot
            ON newer_snapshot.snapshot_id = newer_op.snapshot_id
           AND newer_snapshot.agent_id = newer_op.agent_id
          WHERE newer_op.agent_id = active_op.agent_id
            AND newer_op.status = 'applied'
            AND newer_op.completed_at IS NOT NULL
            AND (
                newer_op.completed_at > active_op.completed_at
                OR (newer_op.completed_at = active_op.completed_at
                    AND newer_snapshot.version > active_snapshot.version)
                OR (newer_op.completed_at = active_op.completed_at
                    AND newer_snapshot.version = active_snapshot.version
                    AND newer_op.operation_id > active_op.operation_id)
            )
      )
"""


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

    def _retention_cutoff_value(self, cutoff: datetime) -> Any:
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("retention cutoff must be timezone-aware")
        utc_cutoff = cutoff.astimezone(timezone.utc)
        if self.dialect == "sqlite":
            return utc_cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")
        return utc_cutoff

    def _retention_before(self, column: str) -> str:
        if self.dialect == "sqlite":
            return f"julianday({column}) < julianday(?)"
        return column + " < ?"

    def retention_counts(self, cutoff: datetime) -> dict[str, int]:
        """Count expired rows without mutation, excluding current state and credentials."""
        value = self._retention_cutoff_value(cutoff)
        session_count = self._execute(
            "SELECT COUNT(*) FROM agent_sessions WHERE ended_at IS NOT NULL AND "
            + self._retention_before("ended_at"),
            (value,),
        ).fetchone()[0]
        operation_predicate = (
            "status IN (" + _TERMINAL_CONFIG_STATUSES + ") AND completed_at IS NOT NULL "
            "AND "
            + self._retention_before("completed_at")
            + " AND operation_id NOT IN ("
            + _LATEST_APPLIED_OPERATION_IDS
            + ")"
        )
        operation_count = self._execute(
            "SELECT COUNT(*) FROM config_operations WHERE " + operation_predicate,
            (value,),
        ).fetchone()[0]
        snapshot_count = self._execute(
            "SELECT COUNT(*) FROM config_snapshots s WHERE "
            + self._retention_before("s.created_at")
            + " "
            "AND s.snapshot_id <> (SELECT newest.snapshot_id FROM config_snapshots newest "
            "ORDER BY newest.created_at DESC, newest.snapshot_id DESC LIMIT 1) "
            "AND NOT EXISTS (SELECT 1 FROM config_operations ref "
            "WHERE ref.snapshot_id = s.snapshot_id AND ref.agent_id = s.agent_id "
            "AND NOT (ref.status IN (" + _TERMINAL_CONFIG_STATUSES + ") "
            "AND ref.completed_at IS NOT NULL AND "
            + self._retention_before("ref.completed_at")
            + " AND ref.operation_id NOT IN ("
            + _LATEST_APPLIED_OPERATION_IDS
            + ")))" ,
            (value, value),
        ).fetchone()[0]
        audit_count = self._execute(
            "SELECT COUNT(*) FROM audit_events WHERE "
            + self._retention_before("occurred_at")
            + " "
            "AND event_type NOT LIKE ?",
            (value, "agent.credential.%"),
        ).fetchone()[0]
        return {
            "sessions": int(session_count),
            "config_operations": int(operation_count),
            "config_snapshots": int(snapshot_count),
            "audit_events": int(audit_count),
        }

    def apply_retention(self, cutoff: datetime) -> dict[str, int]:
        """Delete expired operational rows inside the caller's transaction."""
        value = self._retention_cutoff_value(cutoff)
        sessions = self._execute(
            "DELETE FROM agent_sessions WHERE ended_at IS NOT NULL AND "
            + self._retention_before("ended_at"),
            (value,),
        ).rowcount
        operation_predicate = (
            "status IN (" + _TERMINAL_CONFIG_STATUSES + ") AND completed_at IS NOT NULL "
            "AND "
            + self._retention_before("completed_at")
            + " AND operation_id NOT IN ("
            + _LATEST_APPLIED_OPERATION_IDS
            + ")"
        )
        operations = self._execute(
            "DELETE FROM config_operations WHERE " + operation_predicate,
            (value,),
        ).rowcount
        snapshots = self._execute(
            "DELETE FROM config_snapshots WHERE "
            + self._retention_before("created_at")
            + " "
            "AND snapshot_id <> (SELECT newest.snapshot_id FROM config_snapshots newest "
            "ORDER BY newest.created_at DESC, newest.snapshot_id DESC LIMIT 1) "
            "AND NOT EXISTS (SELECT 1 FROM config_operations ref "
            "WHERE ref.snapshot_id = config_snapshots.snapshot_id "
            "AND ref.agent_id = config_snapshots.agent_id)",
            (value,),
        ).rowcount
        audit_events = self._execute(
            "DELETE FROM audit_events WHERE "
            + self._retention_before("occurred_at")
            + " AND event_type NOT LIKE ?",
            (value, "agent.credential.%"),
        ).rowcount
        return {
            "sessions": int(sessions),
            "config_operations": int(operations),
            "config_snapshots": int(snapshots),
            "audit_events": int(audit_events),
        }

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

    def _dicts(self, cursor: Any) -> List[dict[str, Any]]:
        descriptions = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
        result = []
        for row in rows:
            values = dict(row) if hasattr(row, "keys") else dict(zip(descriptions, row))
            result.append(
                {
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in values.items()
                }
            )
        return result

    def list_admin_agents(self) -> List[dict[str, Any]]:
        """Return a safe operational projection, never selecting credentials or secrets."""
        cursor = self._execute(
            "SELECT a.agent_id, a.display_name, a.enabled, a.created_at, "
            "s.session_id, s.state AS observed_state, s.started_at AS session_started_at, "
            "s.ended_at AS session_ended_at, s.hostname, s.os_version, s.last_heartbeat_at, "
            "s.applied_config_version, s.observed_config_version "
            "FROM agents a LEFT JOIN agent_sessions s ON s.session_id = ("
            "SELECT latest.session_id FROM agent_sessions latest "
            "WHERE latest.agent_id = a.agent_id "
            "ORDER BY latest.started_at DESC, latest.session_id DESC LIMIT 1) "
            "ORDER BY a.agent_id"
        )
        return self._dicts(cursor)

    def get_admin_agent(self, agent_id: str) -> Optional[dict[str, Any]]:
        """Return one agent's safe operational projection, if it exists."""
        cursor = self._execute(
            "SELECT a.agent_id, a.display_name, a.enabled, a.created_at, "
            "s.session_id, s.state AS observed_state, s.started_at AS session_started_at, "
            "s.ended_at AS session_ended_at, s.hostname, s.os_version, s.last_heartbeat_at, "
            "s.applied_config_version, s.observed_config_version "
            "FROM agents a LEFT JOIN agent_sessions s ON s.session_id = ("
            "SELECT latest.session_id FROM agent_sessions latest "
            "WHERE latest.agent_id = a.agent_id "
            "ORDER BY latest.started_at DESC, latest.session_id DESC LIMIT 1) "
            "WHERE a.agent_id = ?",
            (agent_id,),
        )
        rows = self._dicts(cursor)
        return rows[0] if rows else None

    def list_admin_config_operations(
        self, agent_id: Optional[str] = None
    ) -> List[dict[str, Any]]:
        """Return safe operation history joined to its persisted snapshot version."""
        query = (
            "SELECT o.operation_id, o.agent_id, o.status, s.version, "
            "o.requested_at, o.completed_at "
            "FROM config_operations o JOIN config_snapshots s "
            "ON s.snapshot_id = o.snapshot_id AND s.agent_id = o.agent_id"
        )
        parameters: tuple = ()
        if agent_id is not None:
            query += " WHERE o.agent_id = ?"
            parameters = (agent_id,)
        query += " ORDER BY o.requested_at DESC, o.operation_id DESC"
        return self._dicts(self._execute(query, parameters))

    def next_config_version(self, agent_id: str) -> int:
        cursor = self._execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM config_snapshots WHERE agent_id = ?",
            (agent_id,),
        )
        return int(cursor.fetchone()[0])

    def get_admin_config_operation(self, operation_id: str) -> Optional[dict[str, Any]]:
        rows = self._dicts(
            self._execute(
                "SELECT o.operation_id, o.agent_id, o.status, s.version, "
                "o.requested_at, o.completed_at "
                "FROM config_operations o JOIN config_snapshots s "
                "ON s.snapshot_id = o.snapshot_id AND s.agent_id = o.agent_id "
                "WHERE o.operation_id = ?",
                (operation_id,),
            )
        )
        return rows[0] if rows else None

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

    def revoke_active_credentials(self, agent_id: str) -> int:
        """Revoke all currently active credentials for one agent."""
        cursor = self._execute(
            "UPDATE agent_credentials SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE agent_id = ? AND revoked_at IS NULL",
            (agent_id,),
        )
        return cursor.rowcount

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

    def list_equipments(self) -> List[dict[str, Any]]:
        cursor = self._execute(
            "SELECT e.equipment_id, e.name, e.ip_address, e.agent_id, e.created_at, e.updated_at, "
            "a.display_name AS agent_display_name, "
            "s.session_id, s.state AS agent_session_state, "
            "(SELECT COUNT(*) FROM named_opc_configs c WHERE c.equipment_id = e.equipment_id) AS config_count "
            "FROM equipments e "
            "LEFT JOIN agents a ON a.agent_id = e.agent_id "
            "LEFT JOIN agent_sessions s ON s.session_id = ("
            "SELECT latest.session_id FROM agent_sessions latest "
            "WHERE latest.agent_id = e.agent_id "
            "ORDER BY latest.started_at DESC, latest.session_id DESC LIMIT 1) "
            "ORDER BY e.name, e.equipment_id"
        )
        rows = self._dicts(cursor)
        for r in rows:
            if not r.get("agent_id"):
                r["agent_status"] = "unassociated"
            elif r.get("agent_session_state") == "connected" and r.get("session_id") is not None:
                r["agent_status"] = "connected"
            else:
                r["agent_status"] = "disconnected"
        return rows

    def get_equipment(self, equipment_id: str) -> Optional[dict[str, Any]]:
        cursor = self._execute(
            "SELECT e.equipment_id, e.name, e.ip_address, e.agent_id, e.created_at, e.updated_at, "
            "a.display_name AS agent_display_name, "
            "s.session_id, s.state AS agent_session_state, "
            "(SELECT COUNT(*) FROM named_opc_configs c WHERE c.equipment_id = e.equipment_id) AS config_count "
            "FROM equipments e "
            "LEFT JOIN agents a ON a.agent_id = e.agent_id "
            "LEFT JOIN agent_sessions s ON s.session_id = ("
            "SELECT latest.session_id FROM agent_sessions latest "
            "WHERE latest.agent_id = e.agent_id "
            "ORDER BY latest.started_at DESC, latest.session_id DESC LIMIT 1) "
            "WHERE e.equipment_id = ?",
            (equipment_id,),
        )
        rows = self._dicts(cursor)
        if not rows:
            return None
        r = rows[0]
        if not r.get("agent_id"):
            r["agent_status"] = "unassociated"
        elif r.get("agent_session_state") == "connected" and r.get("session_id") is not None:
            r["agent_status"] = "connected"
        else:
            r["agent_status"] = "disconnected"
        return r

    def add_equipment(
        self, equipment_id: str, name: str, ip_address: str, agent_id: Optional[str] = None
    ) -> Equipment:
        self._execute(
            "INSERT INTO equipments(equipment_id, name, ip_address, agent_id) VALUES (?, ?, ?, ?)",
            (equipment_id, name, ip_address, agent_id),
        )
        return Equipment(equipment_id=equipment_id, name=name, ip_address=ip_address, agent_id=agent_id)

    def update_equipment(
        self, equipment_id: str, name: str, ip_address: str, agent_id: Optional[str] = None
    ) -> Optional[Equipment]:
        cursor = self._execute(
            "UPDATE equipments SET name = ?, ip_address = ?, agent_id = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE equipment_id = ?",
            (name, ip_address, agent_id, equipment_id),
        )
        if cursor.rowcount == 0:
            return None
        return Equipment(equipment_id=equipment_id, name=name, ip_address=ip_address, agent_id=agent_id)

    def delete_equipment(self, equipment_id: str) -> bool:
        cursor = self._execute("DELETE FROM equipments WHERE equipment_id = ?", (equipment_id,))
        return cursor.rowcount > 0

    def list_named_configs(self, equipment_id: Optional[str] = None) -> List[dict[str, Any]]:
        query = (
            "SELECT c.config_id, c.name, c.equipment_id, c.agent_id, c.opc_prog_id, c.interval_ms, "
            "c.tags_json, c.created_at, c.updated_at, e.name AS equipment_name "
            "FROM named_opc_configs c "
            "JOIN equipments e ON e.equipment_id = c.equipment_id"
        )
        params: tuple = ()
        if equipment_id is not None:
            query += " WHERE c.equipment_id = ?"
            params = (equipment_id,)
        query += " ORDER BY c.name, c.config_id"
        return self._dicts(self._execute(query, params))

    def get_named_config(self, config_id: str) -> Optional[dict[str, Any]]:
        cursor = self._execute(
            "SELECT c.config_id, c.name, c.equipment_id, c.agent_id, c.opc_prog_id, c.interval_ms, "
            "c.tags_json, c.created_at, c.updated_at, e.name AS equipment_name "
            "FROM named_opc_configs c "
            "JOIN equipments e ON e.equipment_id = c.equipment_id "
            "WHERE c.config_id = ?",
            (config_id,),
        )
        rows = self._dicts(cursor)
        return rows[0] if rows else None

    def add_named_config(
        self,
        config_id: str,
        name: str,
        equipment_id: str,
        opc_prog_id: str,
        interval_ms: int,
        tags_json: str,
        agent_id: Optional[str] = None,
    ) -> NamedOpcConfig:
        self._execute(
            "INSERT INTO named_opc_configs(config_id, name, equipment_id, agent_id, opc_prog_id, interval_ms, tags_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (config_id, name, equipment_id, agent_id, opc_prog_id, interval_ms, tags_json),
        )
        return NamedOpcConfig(
            config_id=config_id,
            name=name,
            equipment_id=equipment_id,
            agent_id=agent_id,
            opc_prog_id=opc_prog_id,
            interval_ms=interval_ms,
            tags_json=tags_json,
        )

    def update_named_config(
        self,
        config_id: str,
        name: str,
        equipment_id: str,
        opc_prog_id: str,
        interval_ms: int,
        tags_json: str,
        agent_id: Optional[str] = None,
    ) -> Optional[NamedOpcConfig]:
        cursor = self._execute(
            "UPDATE named_opc_configs "
            "SET name = ?, equipment_id = ?, agent_id = ?, opc_prog_id = ?, interval_ms = ?, tags_json = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE config_id = ?",
            (name, equipment_id, agent_id, opc_prog_id, interval_ms, tags_json, config_id),
        )
        if cursor.rowcount == 0:
            return None
        return NamedOpcConfig(
            config_id=config_id,
            name=name,
            equipment_id=equipment_id,
            agent_id=agent_id,
            opc_prog_id=opc_prog_id,
            interval_ms=interval_ms,
            tags_json=tags_json,
        )

    def delete_named_config(self, config_id: str) -> bool:
        cursor = self._execute("DELETE FROM named_opc_configs WHERE config_id = ?", (config_id,))
        return cursor.rowcount > 0

    def list_named_configs_for_equipment(self, equipment_id: str) -> List[dict[str, Any]]:
        cursor = self._execute(
            "SELECT config_id, name FROM named_opc_configs WHERE equipment_id = ? ORDER BY name",
            (equipment_id,),
        )
        return self._dicts(cursor)

    def list_pi_mappings(
        self, equipment_id: Optional[str] = None, opc_config_id: Optional[str] = None
    ) -> List[dict[str, Any]]:
        query = (
            "SELECT m.mapping_id, m.mapping_id AS id, m.equipment_id, m.opc_config_id, "
            "m.opc_item_path, m.item_id, m.pi_point_name, "
            "COALESCE(p.point_source, m.point_source) AS point_source, "
            "COALESCE(p.location1, m.location1) AS location1, "
            "p.profile_id, p.enabled AS profile_enabled, "
            "m.publish_interval_ms, m.enabled, m.last_publish_status, m.last_published_at, "
            "m.last_published_value, m.next_publish_due_at, m.last_publish_error, m.failure_count, "
            "m.created_at, m.updated_at, "
            "e.name AS equipment_name, e.agent_id, c.name AS config_name, c.interval_ms AS opc_interval_ms, "
            "c.opc_prog_id "
            "FROM pi_mappings m "
            "JOIN equipments e ON e.equipment_id = m.equipment_id "
            "JOIN named_opc_configs c ON c.config_id = m.opc_config_id "
            "LEFT JOIN pi_profiles p ON p.equipment_id = m.equipment_id AND p.opc_config_id = m.opc_config_id "
        )
        params: list[Any] = []
        clauses: list[str] = []
        if equipment_id:
            clauses.append("m.equipment_id = ?")
            params.append(equipment_id)
        if opc_config_id:
            clauses.append("m.opc_config_id = ?")
            params.append(opc_config_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY m.pi_point_name, m.mapping_id"
        return self._dicts(self._execute(query, tuple(params)))

    def get_pi_mapping(self, mapping_id: str) -> Optional[dict[str, Any]]:
        query = (
            "SELECT m.mapping_id, m.mapping_id AS id, m.equipment_id, m.opc_config_id, "
            "m.opc_item_path, m.item_id, m.pi_point_name, "
            "COALESCE(p.point_source, m.point_source) AS point_source, "
            "COALESCE(p.location1, m.location1) AS location1, "
            "p.profile_id, p.enabled AS profile_enabled, "
            "m.publish_interval_ms, m.enabled, m.last_publish_status, m.last_published_at, "
            "m.last_published_value, m.next_publish_due_at, m.last_publish_error, m.failure_count, "
            "m.created_at, m.updated_at, "
            "e.name AS equipment_name, e.agent_id, c.name AS config_name, c.interval_ms AS opc_interval_ms, "
            "c.opc_prog_id "
            "FROM pi_mappings m "
            "JOIN equipments e ON e.equipment_id = m.equipment_id "
            "JOIN named_opc_configs c ON c.config_id = m.opc_config_id "
            "LEFT JOIN pi_profiles p ON p.equipment_id = m.equipment_id AND p.opc_config_id = m.opc_config_id "
            "WHERE m.mapping_id = ?"
        )
        rows = self._dicts(self._execute(query, (mapping_id,)))
        return rows[0] if rows else None

    def add_pi_mapping(
        self,
        mapping_id: str,
        equipment_id: str,
        opc_config_id: str,
        opc_item_path: str,
        item_id: int,
        pi_point_name: str,
        point_source: str,
        location1: int,
        publish_interval_ms: int,
        enabled: bool = True,
    ) -> PiMapping:
        self._execute(
            "INSERT INTO pi_mappings (mapping_id, equipment_id, opc_config_id, opc_item_path, "
            "item_id, pi_point_name, point_source, location1, publish_interval_ms, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mapping_id,
                equipment_id,
                opc_config_id,
                opc_item_path,
                item_id,
                pi_point_name,
                point_source,
                location1,
                publish_interval_ms,
                1 if enabled else 0,
            ),
        )
        return PiMapping(
            mapping_id=mapping_id,
            equipment_id=equipment_id,
            opc_config_id=opc_config_id,
            opc_item_path=opc_item_path,
            item_id=item_id,
            pi_point_name=pi_point_name,
            point_source=point_source,
            location1=location1,
            publish_interval_ms=publish_interval_ms,
            enabled=enabled,
        )

    def update_pi_mapping(
        self,
        mapping_id: str,
        pi_point_name: str,
        point_source: str,
        location1: int,
        publish_interval_ms: int,
        enabled: bool,
    ) -> Optional[dict[str, Any]]:
        cursor = self._execute(
            "UPDATE pi_mappings SET pi_point_name = ?, point_source = ?, location1 = ?, "
            "publish_interval_ms = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE mapping_id = ?",
            (
                pi_point_name,
                point_source,
                location1,
                publish_interval_ms,
                1 if enabled else 0,
                mapping_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        return self.get_pi_mapping(mapping_id)

    def set_pi_mapping_enabled(self, mapping_id: str, enabled: bool) -> bool:
        cursor = self._execute(
            "UPDATE pi_mappings SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE mapping_id = ?",
            (1 if enabled else 0, mapping_id),
        )
        return cursor.rowcount > 0

    def update_pi_mapping_status(
        self, mapping_id: str, status: str, published_value: Optional[str] = None
    ) -> bool:
        cursor = self._execute(
            "UPDATE pi_mappings SET last_publish_status = ?, last_published_at = CURRENT_TIMESTAMP, "
            "last_published_value = ?, updated_at = CURRENT_TIMESTAMP WHERE mapping_id = ?",
            (status, published_value, mapping_id),
        )
        return cursor.rowcount > 0

    def update_pi_mapping_publication(
        self,
        mapping_id: str,
        status: str,
        published_value: Optional[str] = None,
        next_publish_due_at: Optional[str] = None,
        error: Optional[str] = None,
        failure_count: Optional[int] = None,
    ) -> bool:
        cursor = self._execute(
            "UPDATE pi_mappings SET last_publish_status = ?, last_published_at = CURRENT_TIMESTAMP, "
            "last_published_value = ?, next_publish_due_at = ?, last_publish_error = ?, "
            "failure_count = COALESCE(?, failure_count), updated_at = CURRENT_TIMESTAMP WHERE mapping_id = ?",
            (status, published_value, next_publish_due_at, error, failure_count, mapping_id),
        )
        return cursor.rowcount > 0

    def delete_pi_mapping(self, mapping_id: str) -> bool:
        cursor = self._execute("DELETE FROM pi_mappings WHERE mapping_id = ?", (mapping_id,))
        return cursor.rowcount > 0

    def get_pi_profile(self, equipment_id: str, opc_config_id: str) -> Optional[dict[str, Any]]:
        query = (
            "SELECT p.profile_id, p.profile_id AS id, p.equipment_id, p.opc_config_id, "
            "p.point_source, p.location1, p.enabled, p.created_at, p.updated_at, "
            "e.name AS equipment_name, c.name AS config_name "
            "FROM pi_profiles p "
            "JOIN equipments e ON e.equipment_id = p.equipment_id "
            "JOIN named_opc_configs c ON c.config_id = p.opc_config_id "
            "WHERE p.equipment_id = ? AND p.opc_config_id = ?"
        )
        rows = self._dicts(self._execute(query, (equipment_id, opc_config_id)))
        return rows[0] if rows else None

    def get_pi_profile_by_id(self, profile_id: str) -> Optional[dict[str, Any]]:
        query = (
            "SELECT p.profile_id, p.profile_id AS id, p.equipment_id, p.opc_config_id, "
            "p.point_source, p.location1, p.enabled, p.created_at, p.updated_at, "
            "e.name AS equipment_name, c.name AS config_name "
            "FROM pi_profiles p "
            "JOIN equipments e ON e.equipment_id = p.equipment_id "
            "JOIN named_opc_configs c ON c.config_id = p.opc_config_id "
            "WHERE p.profile_id = ?"
        )
        rows = self._dicts(self._execute(query, (profile_id,)))
        return rows[0] if rows else None

    def list_pi_profiles(self, equipment_id: Optional[str] = None) -> List[dict[str, Any]]:
        query = (
            "SELECT p.profile_id, p.profile_id AS id, p.equipment_id, p.opc_config_id, "
            "p.point_source, p.location1, p.enabled, p.created_at, p.updated_at, "
            "e.name AS equipment_name, c.name AS config_name "
            "FROM pi_profiles p "
            "JOIN equipments e ON e.equipment_id = p.equipment_id "
            "JOIN named_opc_configs c ON c.config_id = p.opc_config_id"
        )
        params: tuple = ()
        if equipment_id:
            query += " WHERE p.equipment_id = ?"
            params = (equipment_id,)
        query += " ORDER BY e.name, c.name"
        return self._dicts(self._execute(query, params))

    def add_pi_profile(
        self,
        profile_id: str,
        equipment_id: str,
        opc_config_id: str,
        point_source: str,
        location1: int,
        enabled: bool = True,
    ) -> PiProfile:
        self._execute(
            "INSERT INTO pi_profiles (profile_id, equipment_id, opc_config_id, point_source, location1, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (profile_id, equipment_id, opc_config_id, point_source, location1, 1 if enabled else 0),
        )
        return PiProfile(
            profile_id=profile_id,
            equipment_id=equipment_id,
            opc_config_id=opc_config_id,
            point_source=point_source,
            location1=location1,
            enabled=enabled,
        )

    def update_pi_profile(
        self,
        profile_id: str,
        point_source: str,
        location1: int,
        enabled: bool,
    ) -> Optional[dict[str, Any]]:
        cursor = self._execute(
            "UPDATE pi_profiles SET point_source = ?, location1 = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE profile_id = ?",
            (point_source, location1, 1 if enabled else 0, profile_id),
        )
        if cursor.rowcount == 0:
            return None
        return self.get_pi_profile_by_id(profile_id)

    def delete_pi_profile(self, profile_id: str) -> bool:
        cursor = self._execute("DELETE FROM pi_profiles WHERE profile_id = ?", (profile_id,))
        return cursor.rowcount > 0

    def deactivate_mappings_for_profile(
        self,
        equipment_id: str,
        opc_config_id: str,
        new_point_source: Optional[str] = None,
        new_location1: Optional[int] = None,
    ) -> int:
        if new_point_source is not None and new_location1 is not None:
            cursor = self._execute(
                "UPDATE pi_mappings SET enabled = 0, point_source = ?, location1 = ?, "
                "last_publish_status = 'Perfil alterado — revalidação necessária', "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE equipment_id = ? AND opc_config_id = ?",
                (new_point_source, new_location1, equipment_id, opc_config_id),
            )
        else:
            cursor = self._execute(
                "UPDATE pi_mappings SET enabled = 0, "
                "last_publish_status = 'Perfil alterado — revalidação necessária', "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE equipment_id = ? AND opc_config_id = ?",
                (equipment_id, opc_config_id),
            )
        return cursor.rowcount
