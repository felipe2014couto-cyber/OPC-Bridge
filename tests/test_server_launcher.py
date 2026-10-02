"""Production launcher validation tests; TLS loading is stubbed, no certs are created."""
from __future__ import annotations

import io
import logging
import ssl
import sys

import pytest

from opc_bridge.server import launcher
from opc_bridge.server.persistence import Database


def valid_environment():
    return {
        "OPC_BRIDGE_HOST": "127.0.0.1",
        "OPC_BRIDGE_PORT": "9443",
        "DATABASE_URL": "postgresql://db-user:db-password@db.internal/opc_bridge",
        "OPC_BRIDGE_TLS_CERTFILE": "/secure/tls/server.crt",
        "OPC_BRIDGE_TLS_KEYFILE": "/secure/tls/server.key",
        "OPC_BRIDGE_LOG_LEVEL": "WARNING",
    }


@pytest.fixture
def valid_tls(monkeypatch):
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", lambda self, cert, key: None)


def test_load_settings_builds_postgres_tls_server_config(valid_tls):
    settings = launcher.load_settings(valid_environment())

    assert settings.server_config.host == "127.0.0.1"
    assert settings.server_config.port == 9443
    assert settings.server_config.persistence.dialect == "postgresql"
    assert isinstance(settings.server_config.persistence, Database)
    assert settings.server_config.certfile == "/secure/tls/server.crt"
    assert settings.server_config.keyfile == "/secure/tls/server.key"
    assert settings.log_level == logging.WARNING


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"DATABASE_URL": ""}, "DATABASE_URL must be set"),
        ({"DATABASE_URL": "sqlite:///prod.db"}, "DATABASE_URL must use PostgreSQL"),
        ({"OPC_BRIDGE_TLS_CERTFILE": ""}, "OPC_BRIDGE_TLS_CERTFILE must be set"),
        ({"OPC_BRIDGE_TLS_KEYFILE": ""}, "OPC_BRIDGE_TLS_KEYFILE must be set"),
    ],
)
def test_load_settings_rejects_missing_or_nonproduction_persistence_and_tls(
    changes, message, valid_tls
):
    environ = valid_environment()
    environ.update(changes)

    with pytest.raises(launcher.LauncherConfigurationError, match=message):
        launcher.load_settings(environ)


def test_load_settings_rejects_invalid_tls_pair_without_echoing_paths(monkeypatch):
    secret_path = "/private/never-log-this.key"

    def fail_load(self, cert, key):
        raise ssl.SSLError("invalid private key at " + key)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", fail_load)
    environ = valid_environment()
    environ["OPC_BRIDGE_TLS_KEYFILE"] = secret_path

    with pytest.raises(launcher.LauncherConfigurationError) as error:
        launcher.load_settings(environ)

    assert secret_path not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"OPC_BRIDGE_PORT": "0"},
        {"OPC_BRIDGE_PORT": "not-a-port"},
        {"OPC_BRIDGE_LOG_LEVEL": "TRACE"},
        {"ADMIN_API_TOKEN": " "},
        {"ADMIN_API_TOKEN": "valid", "ADMIN_API_PORT": "70000"},
    ],
)
def test_load_settings_rejects_invalid_values(changes, valid_tls):
    environ = valid_environment()
    environ.update(changes)

    with pytest.raises(launcher.LauncherConfigurationError):
        launcher.load_settings(environ)


def test_main_refuses_invalid_configuration_before_constructing_server(monkeypatch, capsys):
    secret = "postgresql://user:secret@host/database"
    key_path = "/never/log/private.key"
    monkeypatch.setenv("DATABASE_URL", secret)
    monkeypatch.setenv("OPC_BRIDGE_TLS_CERTFILE", "/secure/server.crt")
    monkeypatch.setenv("OPC_BRIDGE_TLS_KEYFILE", key_path)
    monkeypatch.setattr(launcher, "BridgeServer", lambda config: pytest.fail("server constructed"))
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", lambda *args: (_ for _ in ()).throw(
        ssl.SSLError("could not load " + key_path)
    ))

    assert launcher.main() == 2
    output = capsys.readouterr()
    assert "valid TLS pair" in output.err
    assert secret not in output.err
    assert key_path not in output.err


@pytest.mark.parametrize(
    "missing_variable, message",
    [
        ("DATABASE_URL", "DATABASE_URL must be set"),
        ("OPC_BRIDGE_TLS_CERTFILE", "OPC_BRIDGE_TLS_CERTFILE must be set"),
        ("OPC_BRIDGE_TLS_KEYFILE", "OPC_BRIDGE_TLS_KEYFILE must be set"),
    ],
)
def test_main_refuses_missing_postgres_or_tls_before_server_construction(
    monkeypatch, capsys, missing_variable, message
):
    for name, value in valid_environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing_variable)
    monkeypatch.setattr(launcher, "BridgeServer", lambda config: pytest.fail("server constructed"))
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", lambda self, cert, key: None)

    assert launcher.main() == 2
    assert message in capsys.readouterr().err


def test_configure_logging_writes_to_stdout(monkeypatch):
    settings = launcher.LauncherSettings(
        server_config=None, log_level=logging.ERROR, log_format="%(levelname)s %(message)s"
    )
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    try:
        launcher.configure_logging(settings)
        logging.getLogger("launcher-test").error("journal message")
        assert "ERROR journal message" in stream.getvalue()
        assert root.handlers[0].stream is stream
    finally:
        root.handlers[:] = old_handlers
        root.setLevel(old_level)
