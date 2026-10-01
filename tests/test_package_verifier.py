from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging" / "windows"))
import json
import zipfile

import pytest

from verify_package import PackageVerificationError, verify_package


def make_package(tmp_path, overrides=None):
    config = {
        "server_host": "central.example",
        "server_port": 8443,
        "agent_id": "agent-test",
        "auth_token": "test-token",
        "certfile": "config/ca.pem",
    }
    files = {
        "runtime/python.exe": b"fake runtime",
        "runtime/Lib/site-packages/pywin32_system32/pywintypes38.dll": b"pywin32",
        "src/opc_bridge/agent/client.py": (
            b"ssl.create_default_context(cafile=self._certfile or None)"
        ),
        "src/opc_bridge/agent/service.py": b"agent service",
        "config/agent.default.json": json.dumps(config).encode(),
        "config/ca.pem": b"test CA",
        "install.bat": b"set PYTHON_EXE=runtime\\python.exe\nruntime\\python.exe -m pip",
        "uninstall.bat": b"runtime\\python.exe -m opc_bridge.agent.service remove",
        "requirements-offline.txt": b"pywin32==312\n",
        "wheels/pywin32-312-cp38-cp38-win_amd64.whl": b"wheel",
    }
    files.update(overrides or {})
    manifest = {
        "version": "0.1.0",
        "commit": "a" * 40,
        "architecture": "x64",
        "runtime": "CPython 3.8 Windows embeddable",
    }
    digest = hashlib.sha256()
    for path, value in sorted(files.items()):
        digest.update(path.encode())
        digest.update(value)
    manifest["sha256"] = digest.hexdigest()
    files["manifest.json"] = json.dumps(manifest).encode()
    zip_path = tmp_path / "agent.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return zip_path


def test_verifies_valid_offline_package(tmp_path):
    result = verify_package(str(make_package(tmp_path)), "x64")
    assert result["architecture"] == "x64"


@pytest.mark.parametrize(
    "path",
    [
        "runtime/python.exe",
        "install.bat",
        "uninstall.bat",
        "config/ca.pem",
        "wheels/pywin32-312-cp38-cp38-win_amd64.whl",
    ],
)
def test_rejects_missing_required_package_files(tmp_path, path):
    package = make_package(tmp_path)
    with zipfile.ZipFile(package) as source:
        files = {name: source.read(name) for name in source.namelist() if name != path}
    # Rebuild with the missing member while retaining an intentionally stale digest.
    replacement = tmp_path / "missing.zip"
    with zipfile.ZipFile(replacement, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    with pytest.raises(PackageVerificationError):
        verify_package(str(replacement))


def test_rejects_system_python_fallback(tmp_path):
    package = make_package(tmp_path, {"install.bat": b"where python\nruntime\\python.exe"})
    with pytest.raises(PackageVerificationError, match="fall back to Python"):
        verify_package(str(package))


def test_rejects_empty_bootstrap_token(tmp_path):
    config = {
        "server_host": "central.example",
        "server_port": 8443,
        "agent_id": "agent-test",
        "auth_token": "",
        "certfile": "config/ca.pem",
    }
    package = make_package(tmp_path, {"config/agent.default.json": json.dumps(config).encode()})
    with pytest.raises(PackageVerificationError, match="non-empty token"):
        verify_package(str(package))


def test_rejects_tls_validation_bypass(tmp_path):
    package = make_package(
        tmp_path,
        {"src/opc_bridge/agent/client.py": b"ssl._create_unverified_context()"},
    )
    with pytest.raises(PackageVerificationError, match="CA-based certificate validation"):
        verify_package(str(package))
