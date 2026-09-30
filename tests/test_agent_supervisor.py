from __future__ import annotations

import asyncio
import logging
import sys

import pytest

from opc_bridge.agent.supervisor import (
    AgentSupervisor,
    ProcessWatchdog,
    configure_rotating_logging,
)


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.connected = False
        self.disconnected = False

    async def connect(self):
        if self.fail:
            raise ConnectionError("offline")
        self.connected = True

    async def run_loop(self):
        await asyncio.sleep(0.01)
        raise ConnectionError("closed")

    async def disconnect(self):
        self.disconnected = True

    @property
    def session_id(self):
        return "test"


@pytest.mark.asyncio
async def test_supervisor_reconnects_and_stops():
    clients = []
    supervisor = None

    def factory():
        nonlocal supervisor
        client = FakeClient()
        clients.append(client)
        if len(clients) == 2 and supervisor is not None:
            supervisor.stop()
        return client

    supervisor = AgentSupervisor(factory, min_backoff=0.001, max_backoff=0.002)
    await asyncio.wait_for(supervisor.run(), timeout=1)
    assert len(clients) >= 2
    assert all(client.disconnected for client in clients)


def test_watchdog_restarts_exited_child():
    watchdog = ProcessWatchdog([sys.executable, "-c", "pass"])
    watchdog.start()
    assert watchdog.process is not None
    watchdog.process.wait(timeout=2)
    assert watchdog.check_and_recover()
    assert watchdog.process is not None and watchdog.process.poll() is None
    watchdog.process.terminate()
    watchdog.process.wait(timeout=2)


def test_rotating_structured_log(tmp_path):
    path = tmp_path / "agent.log"
    handler = configure_rotating_logging(str(path), max_bytes=128, backup_count=2)
    try:
        logging.getLogger("test.agent").info("event=%s", "connected")
        for _ in range(20):
            handler.emit(logging.LogRecord(
                "test.agent", logging.INFO, __file__, 1, "event=%s", ("x" * 40,), None
            ))
        handler.flush()
        assert path.exists()
        assert "level=INFO" in path.read_text(encoding="utf-8")
        assert (tmp_path / "agent.log.1").exists()
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
