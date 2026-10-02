"""Daily/manual command-line interface for control-plane retention."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from opc_bridge.server.retention import (
    RetentionConfigurationError,
    database_from_environment,
    execute_retention,
    retention_days_from_environment,
)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "opc-bridge-retention: invalid arguments\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="opc-bridge-retention",
        description="Preview expired control-plane data; pass --apply to delete it.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete the expired rows (without this flag the command is a dry-run)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:  # noqa: UP045 - Python 3.8 support
    try:
        arguments = _parser().parse_args(argv)
        days = retention_days_from_environment(os.environ)
        database = database_from_environment(os.environ)
    except SystemExit as exc:
        return int(exc.code)
    except RetentionConfigurationError as exc:
        print(f"opc-bridge-retention configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        counts, cutoff = execute_retention(database, days, apply=arguments.apply)
    except Exception:  # noqa: BLE001 - database exceptions can reveal DATABASE_URL
        print("opc-bridge-retention: database operation failed", file=sys.stderr)
        return 1

    mode = "apply" if arguments.apply else "dry-run"
    cutoff_text = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    print(
        "opc-bridge-retention mode={} retention_days={} cutoff_utc={} "
        "sessions={} config_operations={} config_snapshots={} audit_events={}".format(
            mode,
            days,
            cutoff_text,
            counts["sessions"],
            counts["config_operations"],
            counts["config_snapshots"],
            counts["audit_events"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
