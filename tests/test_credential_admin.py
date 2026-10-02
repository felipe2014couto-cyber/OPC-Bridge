"""Tests for per-agent credential provisioning without a production database."""
from __future__ import annotations

import hashlib
import io
import sys

import pytest

from opc_bridge.server import credential_admin
from opc_bridge.server.credentials import verify_agent_credential_hash
from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database

TOKEN = "agent-token-used-only-by-test"


def _audit_events(repo, agent_id):
    rows = repo._execute(
        "SELECT event_type, detail_json FROM audit_events WHERE agent_id = ? ORDER BY occurred_at, event_id",
        (agent_id,),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


@pytest.fixture
def database(tmp_path):
    db = sqlite_for_tests(str(tmp_path / "credentials.sqlite"))
    upgrade_database(db)
    return db


def test_provision_creates_agent_with_hash_only_and_audit(database):
    assert credential_admin.provision_credential(database, "agent-a", TOKEN, False) == (
        "agent.credential.created"
    )

    with database.session() as repo:
        agent = repo.get_agent("agent-a")
        credentials = repo.active_credential_hashes("agent-a")
        events = _audit_events(repo, "agent-a")

    expected = "sha256:" + hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()
    assert agent.display_name == "agent-a"
    assert credentials == [expected]
    assert TOKEN not in credentials[0]
    assert verify_agent_credential_hash(hashlib.sha256(TOKEN.encode("utf-8")).digest(), expected)
    assert [event[0] for event in events] == ["agent.credential.created"]
    assert expected not in events[0][1]


def test_existing_agent_requires_explicit_rotation(database):
    credential_admin.provision_credential(database, "agent-a", TOKEN, False)

    with pytest.raises(credential_admin.CredentialAlreadyExistsError):
        credential_admin.provision_credential(database, "agent-a", "replacement", False)

    with database.session() as repo:
        assert len(repo.active_credential_hashes("agent-a")) == 1
        assert [event[0] for event in _audit_events(repo, "agent-a")] == [
            "agent.credential.created"
        ]


def test_rotation_revokes_old_credential_and_persists_only_new_hash(database):
    credential_admin.provision_credential(database, "agent-a", TOKEN, False)
    replacement = "replacement-agent-token"

    assert credential_admin.provision_credential(database, "agent-a", replacement, True) == (
        "agent.credential.rotated"
    )

    with database.session() as repo:
        credentials = repo._execute(
            "SELECT credential_hash, revoked_at FROM agent_credentials "
            "WHERE agent_id = ?",
            ("agent-a",),
        ).fetchall()
        events = _audit_events(repo, "agent-a")

    expected = "sha256:" + hashlib.sha256(replacement.encode("utf-8")).hexdigest()
    assert len(credentials) == 2
    revoked = [row[0] for row in credentials if row[1] is not None]
    active = [row[0] for row in credentials if row[1] is None]
    assert revoked == ["sha256:" + hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()]
    assert active == [expected]
    assert TOKEN not in "".join(row[0] for row in credentials)
    assert sorted(event[0] for event in events) == sorted([
        "agent.credential.created", "agent.credential.rotated"
    ])
    assert all(expected not in event[1] for event in events)


def test_rotation_requires_existing_agent(database):
    with pytest.raises(credential_admin.CredentialAgentNotFoundError):
        credential_admin.provision_credential(database, "missing-agent", TOKEN, True)


@pytest.mark.parametrize(
    "environ, message",
    [
        ({}, "DATABASE_URL must be set"),
        ({"DATABASE_URL": "sqlite:///forbidden.db"}, "DATABASE_URL must use PostgreSQL"),
    ],
)
def test_database_environment_requires_postgresql(environ, message):
    with pytest.raises(credential_admin.CredentialInputError, match=message):
        credential_admin.database_from_environment(environ)


def test_cli_reads_token_from_stdin_and_never_prints_it(database, monkeypatch, capsys):
    token_stream = io.StringIO(TOKEN + "\n")
    monkeypatch.setattr(sys, "stdin", token_stream)
    monkeypatch.setenv("DATABASE_URL", "postgresql://redacted-value")
    monkeypatch.setattr(
        credential_admin,
        "database_from_environment",
        lambda environ: database,
    )

    assert credential_admin.main(["agent-cli-test"]) == 0
    output = capsys.readouterr()
    assert output.out == "agent credential provisioned\n"
    assert TOKEN not in output.out + output.err
    assert hashlib.sha256(TOKEN.encode("utf-8")).hexdigest() not in output.out + output.err
    with database.session() as repo:
        assert len(repo.active_credential_hashes("agent-cli-test")) == 1


def test_cli_does_not_accept_token_argument_or_echo_unknown_arguments(capsys):
    token_as_argument = "must-not-be-echoed-secret"

    assert credential_admin.main(["agent-a", "--token", token_as_argument]) == 2
    output = capsys.readouterr()
    assert token_as_argument not in output.out + output.err


def test_cli_rejects_empty_stdin_without_creating_agent(database, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("DATABASE_URL", "postgresql://redacted-value")
    monkeypatch.setattr(
        credential_admin,
        "database_from_environment",
        lambda environ: database,
    )

    assert credential_admin.main(["agent-empty-token"]) == 2
    assert "token input must not be empty" in capsys.readouterr().err
    with database.session() as repo:
        assert repo.get_agent("agent-empty-token") is None


def test_database_error_text_cannot_leak_url_token_or_hash(database, monkeypatch, capsys):
    class FailingDatabase:
        def session(self):
            raise RuntimeError(
                "DATABASE_URL secret agent token credential hash should-not-print"
            )

    monkeypatch.setattr(sys, "stdin", io.StringIO(TOKEN + "\n"))
    monkeypatch.setenv("DATABASE_URL", "postgresql://url-secret")
    monkeypatch.setattr(
        credential_admin,
        "database_from_environment",
        lambda environ: FailingDatabase(),
    )

    assert credential_admin.main(["agent-db-error"]) == 1
    output = capsys.readouterr()
    assert "database operation failed" in output.err
    for secret in ("url-secret", TOKEN, "should-not-print"):
        assert secret not in output.out + output.err
