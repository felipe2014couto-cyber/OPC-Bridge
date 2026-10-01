"""Explicit migration entry point; call upgrade_database only for approved targets."""
from __future__ import annotations

import sys

from .database import Database
from .migrations import upgrade_database


def main() -> int:
    try:
        database = Database.from_env()
        upgrade_database(database)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
