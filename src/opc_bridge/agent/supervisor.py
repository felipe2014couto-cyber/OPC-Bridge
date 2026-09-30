"""Agent reconnect and OPC worker process supervision."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import subprocess
import time
from typing import Callable

from .client import AgentClient

logger = logging.getLogger(__name__)


def configure_rotating_logging(
    filename: str, max_bytes: int = 5 * 1024 * 1024, backup_count: int = 5,
    level: int = logging.INFO,
) -> logging.Handler:
    """Configure structured key/value logs with bounded disk usage."""
    handler = logging.handlers.RotatingFileHandler(
        filename, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s level=%(levelname)s logger=%(name)s message=%(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    return handler


class ProcessWatchdog:
    """Restart an OPC child process when it exits or stops responding."""

    def __init__(
        self, command: list[str], healthcheck: Callable[[], bool] | None = None,
        check_interval: float = 2.0, timeout: float = 10.0,
    ) -> None:
        self.command = command
        self.healthcheck = healthcheck
        self.check_interval = check_interval
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self._last_healthy = time.monotonic()

    def start(self) -> None:
        self.process = subprocess.Popen(self.command)
        self._last_healthy = time.monotonic()
        logger.info("OPC worker started pid=%s", self.process.pid)

    def check_and_recover(self) -> bool:
        """Return true when a failed worker was restarted."""
        healthy = self.process is not None and self.process.poll() is None
        if healthy and self.healthcheck is not None:
            try:
                healthy = bool(self.healthcheck())
            except Exception:
                logger.exception("OPC worker healthcheck failed")
                healthy = False
        if healthy:
            self._last_healthy = time.monotonic()
            return False
        if self.process is not None and self.process.poll() is None:
            if time.monotonic() - self._last_healthy < self.timeout:
                return False
            self.process.kill()
            self.process.wait()
            logger.warning("OPC worker hung; process terminated")
        self.start()
        logger.warning("OPC worker recovered pid=%s", self.process.pid if self.process else None)
        return True


class AgentSupervisor:
    """Run an AgentClient continuously, reconnecting with exponential backoff."""

    def __init__(
        self, client_factory: Callable[[], AgentClient], min_backoff: float = 1.0,
        max_backoff: float = 60.0, process_watchdog: ProcessWatchdog | None = None,
    ) -> None:
        if min_backoff <= 0 or max_backoff < min_backoff:
            raise ValueError("Invalid reconnect backoff bounds")
        self.client_factory = client_factory
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.process_watchdog = process_watchdog
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        delay = self.min_backoff
        while not self._stop.is_set():
            client = self.client_factory()
            try:
                await client.connect()
                logger.info("Agent connection established session=%s", client.session_id)
                delay = self.min_backoff
                if self.process_watchdog:
                    self.process_watchdog.check_and_recover()
                await client.run_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent connection attempt failed")
            finally:
                await client.disconnect()
            if self.process_watchdog:
                self.process_watchdog.check_and_recover()
            if self._stop.is_set():
                break
            logger.warning("Reconnecting after delay_seconds=%s", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, self.max_backoff)
