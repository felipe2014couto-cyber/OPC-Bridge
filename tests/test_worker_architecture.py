"""Simulated registry, runtime and agent configuration checks; no COM."""
from __future__ import annotations

import json
import struct
from contextlib import nullcontext
from types import ModuleType

import pytest

from opc_bridge.adapters import worker_runtime
from opc_bridge.adapters.da import automation_wrapper_registered
from opc_bridge.adapters.supervised import SupervisedOpcAdapter
from opc_bridge.agent.service import load_config


def fake_pe(architecture):
    data = bytearray(128)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    data[64:68] = b"PE\0\0"
    struct.pack_into("<H", data, 68, 0x8664 if architecture == "x64" else 0x14C)
    return bytes(data)


def mock_registry(monkeypatch, registered):
    import sys

    winreg = ModuleType("winreg")
    winreg.HKEY_CLASSES_ROOT = "HKCR"
    winreg.KEY_READ = 0x20019
    winreg.KEY_WOW64_64KEY = 0x100
    winreg.KEY_WOW64_32KEY = 0x200
    views = {0x100: "x64", 0x200: "x86"}
    accesses = []

    def open_key(root, path, reserved, access):
        accesses.append(views[access & 0x300])
        if views[access & 0x300] not in registered or path.endswith("LocalServer32"):
            raise FileNotFoundError()
        return nullcontext(path)

    winreg.OpenKey = open_key
    winreg.QueryValueEx = lambda key, name: ("{registered-clsid}" if key.endswith("CLSID") else "wrapper.dll", 1)
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    return accesses


@pytest.mark.parametrize("registered,selected,views", [
    ({"x86"}, "x86", ["x64", "x86", "x86"]),
    ({"x64"}, "x64", ["x64", "x64"]),
    ({"x64", "x86"}, "x64", ["x64", "x64"]),
])
def test_auto_selects_registered_view(monkeypatch, tmp_path, caplog, registered, selected, views):
    accesses = mock_registry(monkeypatch, registered)
    for arch, directory in (("x64", "runtime"), ("x86", "runtime-x86")):
        root = tmp_path / directory
        root.mkdir()
        (root / "python.exe").write_bytes(fake_pe(arch))
    with caplog.at_level("INFO"):
        result = worker_runtime.select_worker_runtime("auto", str(tmp_path / "runtime/pythonservice.exe"))
    assert result == (selected, str(tmp_path / ("runtime" if selected == "x64" else "runtime-x86") / "python.exe"))
    assert accesses == views
    assert f"selected={selected}" in caplog.text
    assert "registered wrapper" in caplog.text


@pytest.mark.parametrize("choice,registered", [("x64", {"x86"}), ("x86", {"x64"}), ("auto", set())])
def test_incompatible_choice_does_not_fall_back(monkeypatch, tmp_path, choice, registered):
    accesses = mock_registry(monkeypatch, registered)
    with pytest.raises(ConnectionError, match="not registered"):
        worker_runtime.select_worker_runtime(choice, str(tmp_path / "runtime/python.exe"))
    assert accesses == (["x64", "x86"] if choice == "auto" else [choice])


@pytest.mark.parametrize("value", [None, 64, True, "X64", "x86 ", "amd64", "", []])
def test_configuration_rejects_invalid_architecture(tmp_path, value):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"worker_architecture": value}), encoding="utf-8")
    with pytest.raises(ValueError, match="worker_architecture"):
        load_config(str(path))


def test_configuration_default_is_auto(tmp_path):
    assert load_config(str(tmp_path / "absent.json"))["worker_architecture"] == "auto"


def test_selected_worker_is_used_by_supervised_adapter(monkeypatch, tmp_path):
    from opc_bridge.adapters import supervised

    path = str(tmp_path / "runtime-x86/python.exe")
    monkeypatch.setattr(supervised, "select_worker_runtime", lambda requested, main: ("x86", path))
    adapter = SupervisedOpcAdapter(worker_architecture="auto")
    assert adapter.worker_executable == path
    assert adapter.worker_architecture == "x86"
    with pytest.raises(ValueError, match="bundled runtimes"):
        SupervisedOpcAdapter(worker_architecture="x86", worker_executable="host-python")


def test_missing_and_wrong_runtime_fail(monkeypatch, tmp_path):
    mock_registry(monkeypatch, {"x86"})
    main = str(tmp_path / "runtime/python.exe")
    with pytest.raises(RuntimeError, match="unavailable"):
        worker_runtime.select_worker_runtime("auto", main)
    (tmp_path / "runtime-x86").mkdir()
    (tmp_path / "runtime-x86/python.exe").write_bytes(fake_pe("x64"))
    with pytest.raises(RuntimeError, match="wrong architecture"):
        worker_runtime.select_worker_runtime("auto", main)


def test_registry_access_error_is_safe(monkeypatch):
    import sys

    mock_registry(monkeypatch, {"x64"})

    def denied(*args):
        raise PermissionError("sensitive registry detail")

    monkeypatch.setattr(sys.modules["winreg"], "OpenKey", denied)
    with pytest.raises(ConnectionError, match="Could not verify") as caught:
        automation_wrapper_registered("x64")
    assert "sensitive" not in str(caught.value)


def test_service_preserves_selection_in_config_push_factory(monkeypatch, tmp_path):

    import asyncio

    from opc_bridge.agent import service

    constructors = []

    class Adapter:
        def __init__(self, **options):
            constructors.append(options)

        def disconnect(self):
            pass

    class Supervisor:
        def __init__(self, client_factory):
            self.client_factory = client_factory

        async def run(self):
            self.client_factory()

        def stop(self):
            pass

    def client(**options):
        options["adapter_factory"]()

    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service, "SupervisedOpcAdapter", Adapter)
    monkeypatch.setattr(service, "AgentSupervisor", Supervisor)
    monkeypatch.setattr(service, "AgentClient", client)
    monkeypatch.setattr(service, "configure_rotating_logging", lambda *args, **kwargs: None)
    config = {"worker_architecture": "x86", "auth_token": "simulated", "server_host": "unused",
              "server_port": 8443, "agent_id": "test", "log_file": str(tmp_path / "agent.log")}
    original_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(type(original_policy)())
    try:
        service.run_agent_main(config)
    finally:
        asyncio.set_event_loop_policy(original_policy)
    assert constructors == [{"worker_architecture": "x86", "prog_id": None},
                            {"worker_architecture": "x86"}]


def test_worker_launch_command_uses_selected_interpreter(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from opc_bridge.adapters import supervised

    path = str(tmp_path / "runtime-x86/python.exe")
    monkeypatch.setattr(supervised, "select_worker_runtime", lambda requested, main: ("x86", path))
    commands = []
    connection = object()
    listener = SimpleNamespace(address=("127.0.0.1", 1234),
                               _listener=SimpleNamespace(_socket=SimpleNamespace(fileno=lambda: 1)),
                               accept=lambda: connection, close=lambda: None)
    monkeypatch.setattr(supervised, "Listener", lambda *args, **kwargs: listener)

    def popen(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(pid=42, poll=lambda: None)

    monkeypatch.setattr(supervised.subprocess, "Popen", popen)
    adapter = SupervisedOpcAdapter(worker_architecture="x86")
    adapter._start_worker()
    assert commands == [[path, "-m", "opc_bridge.adapters.da_worker", "--pipe", "127.0.0.1:1234"]]
    assert adapter._conn is connection


def test_host_python_is_not_a_worker_fallback(monkeypatch, tmp_path):
    mock_registry(monkeypatch, {"x64"})
    with pytest.raises(RuntimeError, match="bundled x64 main runtime"):
        worker_runtime.select_worker_runtime("x64", str(tmp_path / "host-python/python.exe"))
