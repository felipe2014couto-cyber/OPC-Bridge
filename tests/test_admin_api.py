"""Read-only administrative API tests using the WSGI contract."""
from __future__ import annotations

import io
import json
from typing import Any

import pytest

from opc_bridge.server.admin import create_app
from opc_bridge.server.persistence import sqlite_for_tests, upgrade_database

ADMIN_TOKEN = "local-admin-token-test-value"
AGENT_SECRET_HASH = "sha256:top-secret-agent-credential-hash"


@pytest.fixture
def database(tmp_path):
    db = sqlite_for_tests(str(tmp_path / "admin.sqlite"))
    upgrade_database(db)
    with db.session() as repo:
        repo.add_agent("agent-a", "Boiler Agent")
        repo.add_credential("agent-a", "credential-a", AGENT_SECRET_HASH)
        repo.add_session("agent-a", "session-a", "boiler-host", "Windows 10")
        repo.update_session_observed(
            "session-a",
            "connected",
            '{"connection":"connected","hostname":"boiler-host",'
            '"os_version":"Windows 10","applied_config_version":2}',
            "2026-10-01T12:00:00Z",
        )
        repo.update_session_applied_version("session-a", 2)
        repo.add_agent("agent-b", "Pump Agent")
        for index, status in enumerate(("applied", "rejected", "expired", "pending"), start=1):
            agent_id = "agent-a" if index < 4 else "agent-b"
            snapshot_id = "snapshot-" + str(index)
            repo.add_snapshot(
                agent_id,
                snapshot_id,
                index,
                json.dumps({"config_version": index, "items": []}),
            )
            operation_id = "operation-" + str(index)
            repo.add_operation(agent_id, operation_id, snapshot_id)
            if status != "pending":
                repo.complete_operation(operation_id, status)
    return db


@pytest.fixture
def app(database, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    return create_app(database)


def request(app, path: str, token: str | None = ADMIN_TOKEN):
    status: list[str] = []
    headers: list[list[tuple[str, str]]] = []

    def start_response(response_status: str, response_headers: list[tuple[str, str]]) -> None:
        status.append(response_status)
        headers.append(response_headers)

    environ: dict[str, Any] = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path.split("?", 1)[0],
        "QUERY_STRING": path.partition("?")[2],
        "wsgi.input": io.BytesIO(),
    }
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = "Bearer " + token
    body = b"".join(app(environ, start_response))
    return status[0], dict(headers[0]), json.loads(body)


def test_health_requires_admin_bearer_token(app):
    status, headers, body = request(app, "/health")

    assert status == "200 OK"
    assert body == {"status": "ok", "service": "opc-bridge", "version": "0.1.0"}
    assert headers["Cache-Control"] == "no-store"


def test_api_refuses_to_create_app_without_admin_token(database, monkeypatch):
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="ADMIN_API_TOKEN"):
        create_app(database)


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_requests_without_valid_bearer_token_receive_401(app, token):
    status, _, body = request(app, "/health", token)

    assert status == "401 Unauthorized"
    assert body == {"error": "unauthorized"}


def test_agent_list_contains_safe_operational_state_only(app):
    status, _, body = request(app, "/api/v1/agents")

    assert status == "200 OK"
    agent = body["agents"][0]
    assert agent["agent_id"] == "agent-a"
    assert agent["state"] == "connected"
    assert agent["last_heartbeat"] == "2026-10-01T12:00:00Z"
    assert agent["current_session"]["session_id"] == "session-a"
    assert AGENT_SECRET_HASH not in json.dumps(body)
    assert "credential_hash" not in json.dumps(body)
    assert "auth_token" not in json.dumps(body)


def test_agent_detail_and_not_found(app):
    status, _, body = request(app, "/api/v1/agents/agent-a")
    missing_status, _, missing_body = request(app, "/api/v1/agents/does-not-exist")

    assert status == "200 OK"
    assert body["agent"]["display_name"] == "Boiler Agent"
    assert body["agent"]["observed_state"]["hostname"] == "boiler-host"
    assert missing_status == "404 Not Found"
    assert missing_body == {"error": "not_found"}


def test_config_operation_history_and_agent_filter(app):
    status, _, all_body = request(app, "/api/v1/config-operations")
    filtered_status, _, filtered_body = request(
        app, "/api/v1/config-operations?agent_id=agent-a"
    )

    assert status == "200 OK"
    assert {row["status"] for row in all_body["operations"]} == {
        "applied",
        "rejected",
        "expired",
        "pending",
    }
    assert all("version" in row and "requested_at" in row for row in all_body["operations"])
    assert filtered_status == "200 OK"
    assert len(filtered_body["operations"]) == 3
    assert {row["agent_id"] for row in filtered_body["operations"]} == {"agent-a"}
    rejected = next(row for row in filtered_body["operations"] if row["status"] == "rejected")
    assert rejected["error"] == "Agent rejected the configuration."

