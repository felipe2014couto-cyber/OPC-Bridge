"""Retention tests use isolated SQLite only; no production database is contacted."""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from opc_bridge.server import retention, retention_cli
from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database

OLD = "2026-03-01 00:00:00"
RECENT = "2026-04-30 00:00:00"
CUTOFF = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
NOW = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)


def _add_snapshot_at(repo, snapshot_id, version, payload, created_at):
    repo._execute(
        "INSERT INTO config_snapshots(snapshot_id, agent_id, version, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (snapshot_id, "agent-a", version, payload, created_at),
    )


@pytest.fixture
def database(tmp_path):
    db = sqlite_for_tests(str(tmp_path / "retention.sqlite"))
    upgrade_database(db)
    with db.session() as repo:
        repo.add_agent("agent-a", "Agent A")
        repo.add_credential("agent-a", "credential-active", "sha256:active-hash")
        repo.add_credential("agent-a", "credential-revoked", "sha256:history-hash")
        repo._execute(
            "UPDATE agent_credentials SET revoked_at = ? WHERE credential_id = ?",
            (OLD, "credential-revoked"),
        )
        repo.add_collection_plan("agent-a", "plan-a", 1000)

        repo.add_session("agent-a", "session-old")
        repo.disconnect_session("session-old", "{}")
        repo._execute("UPDATE agent_sessions SET ended_at = ? WHERE session_id = ?", (OLD, "session-old"))
        repo.add_session("agent-a", "session-recent")
        repo.disconnect_session("session-recent", "{}")
        repo._execute(
            "UPDATE agent_sessions SET ended_at = ? WHERE session_id = ?",
            (RECENT, "session-recent"),
        )
        repo.add_session("agent-a", "session-active")
        repo._execute(
            "UPDATE agent_sessions SET last_heartbeat_at = ?, observed_state_json = ? "
            "WHERE session_id = ?",
            (RECENT, '{"connection":"connected"}', "session-active"),
        )

        _add_snapshot_at(repo, "snapshot-active", 1, '{"current":true}', OLD)
        repo.add_operation("agent-a", "operation-active", "snapshot-active")
        repo.complete_operation("operation-active", "applied")
        repo._execute(
            "UPDATE config_operations SET completed_at = ? WHERE operation_id = ?",
            (OLD, "operation-active"),
        )

        _add_snapshot_at(repo, "snapshot-expired", 2, '{"expired":true}', OLD)
        repo.add_operation("agent-a", "operation-expired", "snapshot-expired")
        repo.complete_operation("operation-expired", "rejected")
        repo._execute(
            "UPDATE config_operations SET completed_at = ? WHERE operation_id = ?",
            (OLD, "operation-expired"),
        )

        _add_snapshot_at(repo, "snapshot-pending", 3, '{"pending":true}', RECENT)
        repo.add_operation("agent-a", "operation-pending", "snapshot-pending")

        repo.add_audit_event("agent-a", "audit-old", "config.applied", "{}")
        repo._execute("UPDATE audit_events SET occurred_at = ? WHERE event_id = ?", (OLD, "audit-old"))
        repo.add_audit_event(
            "agent-a", "audit-credential", "agent.credential.rotated", "{}"
        )
        repo._execute(
            "UPDATE audit_events SET occurred_at = ? WHERE event_id = ?",
            (OLD, "audit-credential"),
        )
        repo.add_audit_event("agent-a", "audit-recent", "config.requested", "{}")
        repo._execute(
            "UPDATE audit_events SET occurred_at = ? WHERE event_id = ?",
            (RECENT, "audit-recent"),
        )
    return db


def test_retention_days_default_and_range_validation():
    assert retention.retention_days_from_environment({}) == 7
    for days in range(1, 8):
        assert retention.retention_days_from_environment({"OPC_BRIDGE_RETENTION_DAYS": str(days)}) == days
    for value in ("0", "8", "one", " 7"):
        with pytest.raises(retention.RetentionConfigurationError):
            retention.retention_days_from_environment({"OPC_BRIDGE_RETENTION_DAYS": value})


def test_cutoff_is_utc_even_when_clock_has_another_offset():
    local_now = dt.datetime(2026, 5, 1, 2, tzinfo=dt.timezone(dt.timedelta(hours=2)))

    cutoff = retention.retention_cutoff(1, local_now)

    assert cutoff == dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
    assert cutoff.utcoffset() == dt.timedelta(0)


def test_cli_defaults_to_dry_run_then_applies_when_flagged(database, monkeypatch, capsys):
    monkeypatch.setenv("OPC_BRIDGE_RETENTION_DAYS", "7")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret-placeholder")
    monkeypatch.setattr(retention_cli, "database_from_environment", lambda environ: database)

    assert retention_cli.main([]) == 0
    dry_run_output = capsys.readouterr().out
    assert "mode=dry-run retention_days=7 cutoff_utc=" in dry_run_output
    assert "secret-placeholder" not in dry_run_output
    with database.session() as repo:
        assert repo.get_session("session-old") is not None

    assert retention_cli.main(["--apply"]) == 0
    apply_output = capsys.readouterr().out
    assert "mode=apply retention_days=7 cutoff_utc=" in apply_output
    with database.session() as repo:
        assert repo.get_session("session-old") is None


def test_cli_refuses_sqlite_database_url(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///must-not-open.db")

    assert retention_cli.main([]) == 2
    assert "DATABASE_URL must use PostgreSQL" in capsys.readouterr().err


def test_cli_database_failure_does_not_echo_secrets(monkeypatch, capsys):
    database_url = "postgresql://database-secret"
    token = "token-secret"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(retention_cli, "database_from_environment", lambda environ: object())
    monkeypatch.setattr(
        retention_cli,
        "execute_retention",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(database_url + " " + token)
        ),
    )

    assert retention_cli.main([]) == 1
    output = capsys.readouterr()
    assert "database operation failed" in output.err
    assert database_url not in output.out + output.err
    assert token not in output.out + output.err


def test_dry_run_counts_by_category_without_deleting(database):
    counts, cutoff = retention.execute_retention(database, 1, apply=False, now=NOW)

    assert cutoff == CUTOFF
    assert counts == {
        "sessions": 1,
        "config_operations": 1,
        "config_snapshots": 1,
        "audit_events": 1,
    }
    with database.session() as repo:
        assert repo.get_session("session-old") is not None
        assert len(repo.list_operations("agent-a")) == 3
        assert len(repo.list_snapshots("agent-a")) == 3


def test_apply_is_idempotent_and_preserves_current_configuration_and_credentials(database):
    first, _ = retention.execute_retention(database, 1, apply=True, now=NOW)
    second, _ = retention.execute_retention(database, 1, apply=True, now=NOW)

    assert first == {
        "sessions": 1,
        "config_operations": 1,
        "config_snapshots": 1,
        "audit_events": 1,
    }
    assert second == {
        "sessions": 0,
        "config_operations": 0,
        "config_snapshots": 0,
        "audit_events": 0,
    }
    with database.session() as repo:
        assert repo.get_agent("agent-a") is not None
        assert repo.get_session("session-active").state == "connected"
        assert repo.get_session("session-recent") is not None
        assert repo.get_session("session-old") is None
        assert repo.active_credential_hashes("agent-a") == ["sha256:active-hash"]
        assert repo.get_credential("credential-revoked").revoked_at is not None
        plans = repo._execute(
            "SELECT plan_id FROM collection_plans WHERE agent_id = ?", ("agent-a",)
        ).fetchall()
        assert [row[0] for row in plans] == ["plan-a"]
        assert {snapshot.snapshot_id for snapshot in repo.list_snapshots("agent-a")} == {
            "snapshot-active", "snapshot-pending"
        }
        assert repo.latest_applied_snapshot("agent-a").snapshot_id == "snapshot-active"
        assert repo.latest_snapshot().snapshot_id == "snapshot-pending"
        assert {operation.operation_id for operation in repo.list_operations("agent-a")} == {
            "operation-active", "operation-pending"
        }
        audit_ids = {
            row[0] for row in repo._execute(
                "SELECT event_id FROM audit_events WHERE agent_id = ?", ("agent-a",)
            ).fetchall()
        }
        assert audit_ids == {"audit-credential", "audit-recent"}


def test_retention_migration_is_idempotent_and_snapshots_remain_immutable(database):
    connection = database._connect()
    try:
        connection.execute(
            "DELETE FROM schema_migrations WHERE revision = ?", ("0003_retention_policy",)
        )
        connection.execute(
            "CREATE TRIGGER config_snapshots_no_delete BEFORE DELETE ON config_snapshots "
            "BEGIN SELECT RAISE(ABORT, 'config snapshots are immutable'); END"
        )
        connection.commit()
    finally:
        connection.close()

    upgrade_database(database)
    upgrade_database(database)
    connection = database._connect()
    try:
        delete_trigger = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'config_snapshots_no_delete'"
        ).fetchone()
        assert delete_trigger is None
    finally:
        connection.close()
    with pytest.raises(sqlite3.IntegrityError, match="immutable"), database.session() as repo:
        repo._execute(
            "UPDATE config_snapshots SET payload_json = ? WHERE snapshot_id = ?",
            ('{"mutated":true}', "snapshot-active"),
        )
