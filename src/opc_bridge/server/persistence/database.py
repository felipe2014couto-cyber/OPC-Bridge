"""DB-API database lifecycle. Production URLs must point to PostgreSQL."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:
    from .repositories import PersistenceRepository


class Database:
    """Open short-lived transaction connections from a PostgreSQL production URL."""

    def __init__(self, database_url: str, *, test_sqlite_path: Optional[str] = None) -> None:
        self.database_url = database_url
        self._sqlite_path = test_sqlite_path
        if test_sqlite_path is None and not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production DATABASE_URL must use PostgreSQL")

    @classmethod
    def sqlite_for_tests(cls, path: str) -> Database:
        """Create an explicitly test-only SQLite database."""
        instance = cls.__new__(cls)
        instance.database_url = "sqlite test database"
        instance._sqlite_path = path
        return instance

    @classmethod
    def from_env(cls) -> Database:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL must be set to a PostgreSQL URL")
        return cls(url)

    @property
    def dialect(self) -> str:
        return "sqlite" if self._sqlite_path is not None else "postgresql"

    def _connect(self):
        if self._sqlite_path is not None:
            connection = sqlite3.connect(self._sqlite_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL persistence requires the optional 'persistence' dependencies"
            ) from exc
        return psycopg2.connect(self.database_url)

    @contextmanager
    def session(self) -> Iterator[PersistenceRepository]:
        from .repositories import PersistenceRepository

        connection = self._connect()
        try:
            yield PersistenceRepository(connection, self.dialect)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def database_from_env() -> Database:
    """Construct a production PostgreSQL connection factory from DATABASE_URL."""
    return Database.from_env()


def sqlite_for_tests(path: str) -> Database:
    """Explicit SQLite factory reserved for isolated tests."""
    return Database.sqlite_for_tests(path)
