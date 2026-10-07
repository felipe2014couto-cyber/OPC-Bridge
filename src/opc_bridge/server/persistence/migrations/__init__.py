"""Versioned, explicit control-plane database migrations."""
from __future__ import annotations

from typing import Any, List

REVISION = "0007_pi_profiles"
_PI_PROFILES_REVISION = "0007_pi_profiles"
_PI_TRACKING_REVISION = "0006_pi_publication_tracking"
_PI_MAPPINGS_REVISION = "0005_pi_mappings"
_EQUIPMENT_REVISION = "0004_equipment_and_opc_configs"
_RETENTION_REVISION = "0003_retention_policy"
_BRIDGE_STATE_REVISION = "0002_bridge_server_state"
_INITIAL_REVISION = "0001_initial"


_SCHEMA: List[str] = [
    """CREATE TABLE agents (
        agent_id VARCHAR(128) PRIMARY KEY,
        display_name VARCHAR(255) NOT NULL,
        created_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
    )""",
    """CREATE TABLE agent_credentials (
        credential_id VARCHAR(128) PRIMARY KEY,
        agent_id VARCHAR(128) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
        credential_hash VARCHAR(512) NOT NULL CHECK (length(credential_hash) > 0),
        created_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        revoked_at __TIMESTAMP__,
        UNIQUE (credential_id, agent_id)
    )""",
    """CREATE TABLE agent_sessions (
        session_id VARCHAR(128) PRIMARY KEY,
        agent_id VARCHAR(128) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
        started_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at __TIMESTAMP__,
        applied_config_version INTEGER,
        observed_config_version INTEGER
    )""",
    """CREATE TABLE collection_plans (
        plan_id VARCHAR(128) PRIMARY KEY,
        agent_id VARCHAR(128) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
        interval_ms INTEGER NOT NULL CHECK (interval_ms > 0),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        created_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (plan_id, agent_id)
    )""",
    """CREATE TABLE config_snapshots (
        snapshot_id VARCHAR(128) PRIMARY KEY,
        agent_id VARCHAR(128) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
        version INTEGER NOT NULL CHECK (version >= 0),
        payload_json TEXT NOT NULL,
        created_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (agent_id, version),
        UNIQUE (snapshot_id, agent_id)
    )""",
    """CREATE TABLE config_operations (
        operation_id VARCHAR(128) PRIMARY KEY,
        agent_id VARCHAR(128) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
        snapshot_id VARCHAR(128) NOT NULL,
        plan_id VARCHAR(128),
        status VARCHAR(32) NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'applied', 'rejected', 'failed', 'expired')),
        requested_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at __TIMESTAMP__,
        FOREIGN KEY (snapshot_id, agent_id)
            REFERENCES config_snapshots(snapshot_id, agent_id) ON DELETE RESTRICT,
        FOREIGN KEY (plan_id, agent_id)
            REFERENCES collection_plans(plan_id, agent_id) ON DELETE RESTRICT
    )""",
    """CREATE TABLE audit_events (
        event_id VARCHAR(128) PRIMARY KEY,
        agent_id VARCHAR(128) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
        event_type VARCHAR(128) NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        occurred_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
]


def _apply_initial(cursor: Any, database: Any, marker: str, timestamp_type: str) -> None:
    for statement in _SCHEMA:
        statement = statement.replace("__TIMESTAMP__", timestamp_type)
        cursor.execute(statement)
    if database.dialect == "sqlite":
        cursor.execute(
            "CREATE TRIGGER config_snapshots_no_update BEFORE UPDATE ON config_snapshots "
            "BEGIN SELECT RAISE(ABORT, 'config snapshots are immutable'); END"
        )
        cursor.execute(
            "CREATE TRIGGER config_snapshots_no_delete BEFORE DELETE ON config_snapshots "
            "BEGIN SELECT RAISE(ABORT, 'config snapshots are immutable'); END"
        )
    else:
        cursor.execute(
            "CREATE FUNCTION reject_config_snapshot_mutation() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'config snapshots are immutable'; END; $$ LANGUAGE plpgsql"
        )
        cursor.execute(
            "CREATE TRIGGER config_snapshots_immutable BEFORE UPDATE OR DELETE "
            "ON config_snapshots FOR EACH ROW EXECUTE FUNCTION reject_config_snapshot_mutation()"
        )
    cursor.execute("INSERT INTO schema_migrations(revision) VALUES (" + marker + ")", (_INITIAL_REVISION,))


def _apply_bridge_state(cursor: Any, database: Any, marker: str, timestamp_type: str) -> None:
    for statement in (
        "ALTER TABLE agent_sessions ADD COLUMN hostname VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE agent_sessions ADD COLUMN os_version VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE agent_sessions ADD COLUMN state VARCHAR(32) NOT NULL DEFAULT 'connected'",
        "ALTER TABLE agent_sessions ADD COLUMN last_heartbeat_at " + timestamp_type,
        "ALTER TABLE agent_sessions ADD COLUMN observed_state_json TEXT NOT NULL DEFAULT '{}'",
    ):
        cursor.execute(statement)
    if database.dialect == "sqlite":
        cursor.execute(
            "CREATE TABLE config_operations_v2 ("
            "operation_id VARCHAR(128) PRIMARY KEY, agent_id VARCHAR(128) NOT NULL "
            "REFERENCES agents(agent_id) ON DELETE CASCADE, snapshot_id VARCHAR(128) NOT NULL, "
            "plan_id VARCHAR(128), status VARCHAR(32) NOT NULL DEFAULT 'pending' "
            "CHECK (status IN ('pending', 'applied', 'rejected', 'failed', 'expired')), "
            "requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, "
            "FOREIGN KEY (snapshot_id, agent_id) REFERENCES config_snapshots(snapshot_id, agent_id) "
            "ON DELETE RESTRICT, FOREIGN KEY (plan_id, agent_id) REFERENCES "
            "collection_plans(plan_id, agent_id) ON DELETE RESTRICT)"
        )
        cursor.execute(
            "INSERT INTO config_operations_v2 SELECT operation_id, agent_id, snapshot_id, "
            "plan_id, status, requested_at, completed_at FROM config_operations"
        )
        cursor.execute("DROP TABLE config_operations")
        cursor.execute("ALTER TABLE config_operations_v2 RENAME TO config_operations")
    else:
        cursor.execute("ALTER TABLE config_operations DROP CONSTRAINT config_operations_status_check")
        cursor.execute(
            "ALTER TABLE config_operations ADD CONSTRAINT config_operations_status_check "
            "CHECK (status IN ('pending', 'applied', 'rejected', 'failed', 'expired'))"
        )
    cursor.execute(
        "INSERT INTO schema_migrations(revision) VALUES (" + marker + ")",
        (_BRIDGE_STATE_REVISION,),
    )


def _apply_retention_policy(cursor: Any, database: Any, marker: str) -> None:
    if database.dialect == "sqlite":
        cursor.execute("DROP TRIGGER IF EXISTS config_snapshots_no_delete")
    else:
        cursor.execute("DROP TRIGGER IF EXISTS config_snapshots_immutable ON config_snapshots")
        cursor.execute(
            "CREATE TRIGGER config_snapshots_immutable BEFORE UPDATE ON config_snapshots "
            "FOR EACH ROW EXECUTE FUNCTION reject_config_snapshot_mutation()"
        )
    cursor.execute(
        "INSERT INTO schema_migrations(revision) VALUES (" + marker + ")",
        (_RETENTION_REVISION,),
    )


def _apply_equipment_and_opc_configs(cursor: Any, database: Any, marker: str, timestamp_type: str) -> None:
    statements = [
        """CREATE TABLE equipments (
            equipment_id VARCHAR(128) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            ip_address VARCHAR(128) NOT NULL,
            agent_id VARCHAR(128) REFERENCES agents(agent_id) ON DELETE SET NULL,
            created_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE named_opc_configs (
            config_id VARCHAR(128) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            equipment_id VARCHAR(128) NOT NULL REFERENCES equipments(equipment_id) ON DELETE CASCADE,
            agent_id VARCHAR(128) REFERENCES agents(agent_id) ON DELETE SET NULL,
            opc_prog_id VARCHAR(256) NOT NULL,
            interval_ms INTEGER NOT NULL CHECK (interval_ms >= 1000 AND interval_ms <= 60000),
            tags_json TEXT NOT NULL,
            created_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    ]
    for statement in statements:
        statement = statement.replace("__TIMESTAMP__", timestamp_type)
        cursor.execute(statement)
    cursor.execute(
        "INSERT INTO schema_migrations(revision) VALUES (" + marker + ")",
        (_EQUIPMENT_REVISION,),
    )


def _apply_pi_mappings(cursor: Any, database: Any, marker: str, timestamp_type: str) -> None:
    statements = [
        """CREATE TABLE pi_mappings (
            mapping_id VARCHAR(128) PRIMARY KEY,
            equipment_id VARCHAR(128) NOT NULL REFERENCES equipments(equipment_id) ON DELETE CASCADE,
            opc_config_id VARCHAR(128) NOT NULL REFERENCES named_opc_configs(config_id) ON DELETE CASCADE,
            opc_item_path VARCHAR(512) NOT NULL,
            item_id INTEGER NOT NULL DEFAULT 0,
            pi_point_name VARCHAR(255) NOT NULL,
            point_source VARCHAR(64) NOT NULL DEFAULT '',
            location1 INTEGER NOT NULL DEFAULT 0,
            publish_interval_ms INTEGER NOT NULL CHECK (publish_interval_ms >= 1000 AND publish_interval_ms <= 60000),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            last_publish_status VARCHAR(64) NOT NULL DEFAULT 'Não configurado',
            last_published_at __TIMESTAMP__,
            last_published_value TEXT,
            created_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (opc_config_id, opc_item_path)
        )""",
    ]
    for statement in statements:
        statement = statement.replace("__TIMESTAMP__", timestamp_type)
        cursor.execute(statement)
    cursor.execute(
        "INSERT INTO schema_migrations(revision) VALUES (" + marker + ")",
        (_PI_MAPPINGS_REVISION,),
    )


def _apply_pi_publication_tracking(
    cursor: Any, database: Any, marker: str, timestamp_type: str
) -> None:
    statements = [
        "ALTER TABLE pi_mappings ADD COLUMN next_publish_due_at __TIMESTAMP__",
        "ALTER TABLE pi_mappings ADD COLUMN last_publish_error TEXT",
        "ALTER TABLE pi_mappings ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0",
    ]
    for statement in statements:
        statement = statement.replace("__TIMESTAMP__", timestamp_type)
        cursor.execute(statement)
    cursor.execute(
        "INSERT INTO schema_migrations(revision) VALUES (" + marker + ")",
        (_PI_TRACKING_REVISION,),
    )


def _apply_pi_profiles(cursor: Any, database: Any, marker: str, timestamp_type: str) -> None:
    statements = [
        """CREATE TABLE pi_profiles (
            profile_id VARCHAR(128) PRIMARY KEY,
            equipment_id VARCHAR(128) NOT NULL REFERENCES equipments(equipment_id) ON DELETE CASCADE,
            opc_config_id VARCHAR(128) NOT NULL REFERENCES named_opc_configs(config_id) ON DELETE CASCADE,
            point_source VARCHAR(64) NOT NULL,
            location1 INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            created_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at __TIMESTAMP__ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (equipment_id, opc_config_id)
        )""",
        """UPDATE pi_mappings SET enabled = 0
        WHERE (equipment_id, opc_config_id) NOT IN (
            SELECT equipment_id, opc_config_id FROM pi_profiles WHERE enabled = 1
        )""",
    ]
    for statement in statements:
        statement = statement.replace("__TIMESTAMP__", timestamp_type)
        cursor.execute(statement)
    cursor.execute(
        "INSERT INTO schema_migrations(revision) VALUES (" + marker + ")",
        (_PI_PROFILES_REVISION,),
    )


def upgrade_database(database: Any) -> None:
    """Apply pending migration revisions to an isolated target database."""
    connection = database._connect()
    try:
        cursor = connection.cursor()
        if database.dialect == "sqlite":
            cursor.execute("BEGIN")
        timestamp_type = "TIMESTAMPTZ" if database.dialect == "postgresql" else "TEXT"
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(revision VARCHAR(64) PRIMARY KEY, applied_at "
            + timestamp_type
            + " NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        marker = "%s" if database.dialect == "postgresql" else "?"
        cursor.execute("SELECT revision FROM schema_migrations")
        applied = {row[0] for row in cursor.fetchall()}
        if _INITIAL_REVISION not in applied:
            _apply_initial(cursor, database, marker, timestamp_type)
        if _BRIDGE_STATE_REVISION not in applied:
            _apply_bridge_state(cursor, database, marker, timestamp_type)
        if _RETENTION_REVISION not in applied:
            _apply_retention_policy(cursor, database, marker)
        if _EQUIPMENT_REVISION not in applied:
            _apply_equipment_and_opc_configs(cursor, database, marker, timestamp_type)
        if _PI_MAPPINGS_REVISION not in applied:
            _apply_pi_mappings(cursor, database, marker, timestamp_type)
        if _PI_TRACKING_REVISION not in applied:
            _apply_pi_publication_tracking(cursor, database, marker, timestamp_type)
        if _PI_PROFILES_REVISION not in applied:
            _apply_pi_profiles(cursor, database, marker, timestamp_type)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
