from __future__ import annotations

import sqlite3

import pytest

from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database
from opc_bridge.server.persistence.models import (
    Agent,
    AgentCredential,
    AgentSession,
    AuditEvent,
    CollectionPlan,
    ConfigOperation,
    ConfigSnapshot,
)


@pytest.fixture
def database(tmp_path):
    db = sqlite_for_tests(str(tmp_path / "control-plane.sqlite"))
    upgrade_database(db)
    yield db


def test_migration_creates_all_models_and_is_safe_to_rerun(database):
    upgrade_database(database)
    with database.session() as repo:
        assert repo._execute("SELECT revision FROM schema_migrations").fetchone()[0] == "0001_initial"
        tables = {
            row[0]
            for row in repo._execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert {
        "agents",
        "agent_credentials",
        "agent_sessions",
        "collection_plans",
        "config_snapshots",
        "config_operations",
        "audit_events",
    }.issubset(tables)


def test_failed_migration_rolls_back_partial_schema(tmp_path):
    db = sqlite_for_tests(str(tmp_path / "migration-rollback.sqlite"))
    connection = db._connect()
    connection.execute("CREATE TABLE audit_events (collision INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        upgrade_database(db)

    connection = db._connect()
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "agents" not in tables
        assert "schema_migrations" not in tables
        assert "audit_events" in tables
    finally:
        connection.close()


def test_creation_isolation_and_hash_only_credentials(database):
    with database.session() as repo:
        assert isinstance(repo.add_agent("agent-a", "Agent A"), Agent)
        repo.add_agent("agent-b", "Agent B")
        assert isinstance(repo.add_credential("agent-a", "cred-a", "sha256:deadbeef"), AgentCredential)
        assert isinstance(repo.add_session("agent-a", "session-a"), AgentSession)
        assert isinstance(repo.add_collection_plan("agent-a", "plan-a", 1000), CollectionPlan)
        assert isinstance(repo.add_snapshot("agent-a", "snapshot-a", 1, '{"items":[]}'), ConfigSnapshot)
        assert isinstance(repo.add_operation("agent-a", "operation-a", "snapshot-a", "plan-a"), ConfigOperation)
        assert isinstance(repo.add_audit_event("agent-a", "event-a", "config.created"), AuditEvent)
        assert repo._execute("PRAGMA table_info(agent_credentials)").fetchall()
        credential_columns = {
            row[1] for row in repo._execute("PRAGMA table_info(agent_credentials)").fetchall()
        }
        assert "credential_hash" in credential_columns
        assert "secret" not in credential_columns
        assert "token" not in credential_columns
        assert repo.get_credential("cred-a").credential_hash == "sha256:deadbeef"

    with database.session() as repo:
        assert repo.get_agent("agent-a").display_name == "Agent A"
        assert [row.agent_id for row in repo.list_snapshots("agent-a")] == ["agent-a"]
        assert repo.list_snapshots("agent-b") == []
        assert [row.agent_id for row in repo.list_operations("agent-a")] == ["agent-a"]


def test_rejects_cross_agent_snapshot_or_plan_links_in_repository_and_sql(database):
    with database.session() as repo:
        repo.add_agent("agent-a", "Agent A")
        repo.add_agent("agent-b", "Agent B")
        repo.add_collection_plan("agent-b", "plan-b", 500)
        repo.add_snapshot("agent-a", "snapshot-a", 1, "{}")
        repo.add_snapshot("agent-b", "snapshot-b", 1, "{}")

    with pytest.raises(sqlite3.IntegrityError), database.session() as repo:
        repo.add_operation("agent-a", "wrong-snapshot", "snapshot-b")
    with pytest.raises(sqlite3.IntegrityError), database.session() as repo:
        repo.add_operation("agent-a", "wrong-plan", "snapshot-a", "plan-b")

    connection = database._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO config_operations(operation_id, agent_id, snapshot_id) VALUES (?, ?, ?)",
                ("direct-cross-link", "agent-a", "snapshot-b"),
            )
    finally:
        connection.close()


def test_snapshots_are_immutable_at_database_boundary(database):
    with database.session() as repo:
        repo.add_agent("agent-a", "Agent A")
        repo.add_snapshot("agent-a", "snapshot-a", 1, "{}")

    connection = database._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE config_snapshots SET payload_json = '{}' WHERE snapshot_id = ?",
                ("snapshot-a",),
            )
    finally:
        connection.close()
    with database.session() as repo:
        assert repo.list_snapshots("agent-a")[0].payload_json == "{}"


def test_failed_transaction_rolls_back(database):
    with pytest.raises(sqlite3.IntegrityError), database.session() as repo:
        repo.add_agent("agent-a", "Agent A")
        repo.add_operation("agent-a", "bad-operation", "missing-snapshot")
    with database.session() as repo:
        assert repo.get_agent("agent-a") is None


def test_database_survives_reopen(tmp_path):
    path = str(tmp_path / "restart.sqlite")
    first = sqlite_for_tests(path)
    upgrade_database(first)
    with first.session() as repo:
        repo.add_agent("agent-a", "Agent A")
        repo.add_credential("agent-a", "cred-a", "sha256:deadbeef")
        repo.add_session("agent-a", "session-a")
        repo.add_collection_plan("agent-a", "plan-a", 1000)
        repo.add_snapshot("agent-a", "snapshot-a", 1, '{"x":1}')
        repo.add_operation("agent-a", "operation-a", "snapshot-a", "plan-a")
        repo.add_audit_event("agent-a", "event-a", "config.created")

    reopened = sqlite_for_tests(path)
    with reopened.session() as repo:
        snapshots = repo.list_snapshots("agent-a")
        assert len(snapshots) == 1
        assert snapshots[0].payload_json == '{"x":1}'
        assert repo.get_agent("agent-a").display_name == "Agent A"
        assert repo.get_credential("cred-a").credential_hash == "sha256:deadbeef"
        assert repo._execute(
            "SELECT session_id FROM agent_sessions WHERE agent_id = ?", ("agent-a",)
        ).fetchone()[0] == "session-a"
        assert repo._execute(
            "SELECT plan_id FROM collection_plans WHERE agent_id = ?", ("agent-a",)
        ).fetchone()[0] == "plan-a"
        assert repo.list_operations("agent-a")[0].operation_id == "operation-a"
        assert repo._execute(
            "SELECT event_id FROM audit_events WHERE agent_id = ?", ("agent-a",)
        ).fetchone()[0] == "event-a"


def test_production_database_url_requires_postgresql(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unsafe.db")
    with pytest.raises(ValueError, match="PostgreSQL"):
        from opc_bridge.server.persistence import database_from_env

        database_from_env()
