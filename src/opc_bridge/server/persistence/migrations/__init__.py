"""Versioned, explicit control-plane database migrations."""
from __future__ import annotations

from typing import Any, List

REVISION = "0001_initial"

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
            CHECK (status IN ('pending', 'applied', 'rejected', 'failed')),
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
        cursor.execute("SELECT revision FROM schema_migrations WHERE revision = " + marker, (REVISION,))
        if cursor.fetchone() is None:
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
            cursor.execute(
                "INSERT INTO schema_migrations(revision) VALUES (" + marker + ")", (REVISION,)
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
