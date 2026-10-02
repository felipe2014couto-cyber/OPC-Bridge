"""Safely provision per-agent credentials from standard input."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Mapping, Optional, Sequence

from opc_bridge.server.credentials import encode_agent_credential
from opc_bridge.server.persistence import Database


class CredentialAlreadyExistsError(Exception):
    """An existing agent requires explicit rotation before changing credentials."""


class CredentialAgentNotFoundError(Exception):
    """Rotation was requested for an agent that does not exist."""


class CredentialInputError(ValueError):
    """A safely describable command input/configuration error."""


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse includes unknown arguments in its default diagnostic; they could be secrets.
        self.print_usage(sys.stderr)
        self.exit(2, "credential command: invalid arguments\n")


def database_from_environment(environ: Mapping[str, str]) -> Database:
    """Require a production PostgreSQL URL without ever including it in errors."""
    database_url = environ.get("DATABASE_URL", "")
    if not database_url or not database_url.strip():
        raise CredentialInputError("DATABASE_URL must be set to a PostgreSQL URL")
    if database_url != database_url.strip() or not database_url.startswith(
        ("postgresql://", "postgres://")
    ):
        raise CredentialInputError("DATABASE_URL must use PostgreSQL")
    return Database(database_url)


def _validate_agent_id(agent_id: str) -> None:
    if (
        not agent_id
        or len(agent_id) > 128
        or agent_id != agent_id.strip()
        or any(character.isspace() or not character.isprintable() for character in agent_id)
    ):
        raise CredentialInputError("agent_id must be 1-128 printable characters without whitespace")


def provision_credential(database: Database, agent_id: str, token: str, rotate: bool) -> str:
    """Create an agent credential or atomically replace all active credentials."""
    _validate_agent_id(agent_id)
    if not token:
        raise CredentialInputError("token input must not be empty")

    credential_id = str(uuid.uuid4())
    credential_hash = encode_agent_credential(token)
    with database.session() as repo:
        agent = repo.get_agent(agent_id)
        if agent is None:
            if rotate:
                raise CredentialAgentNotFoundError
            repo.add_agent(agent_id, agent_id)
            event_type = "agent.credential.created"
        else:
            if not rotate:
                raise CredentialAlreadyExistsError
            repo.revoke_active_credentials(agent_id)
            event_type = "agent.credential.rotated"

        repo.add_credential(agent_id, credential_id, credential_hash)
        repo.add_audit_event(
            agent_id,
            str(uuid.uuid4()),
            event_type,
            json.dumps({"credential_id": credential_id}, sort_keys=True),
        )
    return event_type


def _read_token(stream) -> str:
    if stream.isatty():
        raise CredentialInputError(
            "provide the token through redirected stdin; interactive terminal input is disabled"
        )
    token = stream.read()
    if token.endswith("\n"):
        token = token[:-1]
        if token.endswith("\r"):
            token = token[:-1]
    if "\n" in token or "\r" in token:
        raise CredentialInputError("stdin must contain exactly one token line")
    if not token:
        raise CredentialInputError("token input must not be empty")
    return token


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="opc-bridge-agent-credential",
        description="Provision one agent credential; the token is read from stdin.",
    )
    parser.add_argument("agent_id", help="agent identifier (1-128 printable characters)")
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="explicitly revoke active credentials and replace them",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:  # noqa: UP045 - Python 3.8 support
    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    try:
        _validate_agent_id(arguments.agent_id)
        database = database_from_environment(os.environ)
        token = _read_token(sys.stdin)
        event_type = provision_credential(database, arguments.agent_id, token, arguments.rotate)
    except CredentialAlreadyExistsError:
        print("agent already has a credential; use --rotate to replace it", file=sys.stderr)
        return 3
    except CredentialAgentNotFoundError:
        print("agent does not exist; omit --rotate to create it", file=sys.stderr)
        return 4
    except CredentialInputError as exc:
        print(f"credential command: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - hide driver details that may contain credentials
        # Driver exceptions may contain DATABASE_URL; do not emit their text or traceback.
        print("credential command: database operation failed", file=sys.stderr)
        return 1

    if event_type == "agent.credential.created":
        print("agent credential provisioned")
    else:
        print("agent credential rotated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
