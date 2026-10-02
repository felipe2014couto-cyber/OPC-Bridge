"""Operational data-retention policy for the control-plane database."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from opc_bridge.server.persistence import Database

DEFAULT_RETENTION_DAYS = 7
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 7


class RetentionConfigurationError(ValueError):
    """A safe, operator-facing retention configuration error."""


def retention_days_from_environment(environ) -> int:
    raw = environ.get("OPC_BRIDGE_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    if not raw or raw != raw.strip():
        raise RetentionConfigurationError("OPC_BRIDGE_RETENTION_DAYS must be an integer from 1 to 7")
    try:
        days = int(raw)
    except (TypeError, ValueError):
        raise RetentionConfigurationError(
            "OPC_BRIDGE_RETENTION_DAYS must be an integer from 1 to 7"
        ) from None
    if not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
        raise RetentionConfigurationError("OPC_BRIDGE_RETENTION_DAYS must be from 1 to 7")
    return days


def retention_cutoff(days: int, now: Optional[datetime] = None) -> datetime:  # noqa: UP045 - Python 3.8 support
    if not MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
        raise RetentionConfigurationError("retention days must be from 1 to 7")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise RetentionConfigurationError("retention time must include a timezone")
    return current_time.astimezone(timezone.utc) - timedelta(days=days)


def database_from_environment(environ) -> Database:
    database_url = environ.get("DATABASE_URL", "")
    if not database_url or not database_url.strip():
        raise RetentionConfigurationError("DATABASE_URL must be set to a PostgreSQL URL")
    if database_url != database_url.strip() or not database_url.startswith(
        ("postgresql://", "postgres://")
    ):
        raise RetentionConfigurationError("DATABASE_URL must use PostgreSQL")
    return Database(database_url)


def execute_retention(
    database: Database,
    days: int,
    apply: bool = False,
    now: Optional[datetime] = None,  # noqa: UP045 - Python 3.8 support
) -> tuple[dict[str, int], datetime]:
    """Preview or apply retention in one transaction and return counts plus UTC cutoff."""
    cutoff = retention_cutoff(days, now)
    with database.session() as repo:
        counts = repo.apply_retention(cutoff) if apply else repo.retention_counts(cutoff)
    return counts, cutoff
