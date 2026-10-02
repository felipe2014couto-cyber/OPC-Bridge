"""In-memory package fixtures; no distribution ZIP or runtime is built."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_worker_architecture import fake_pe

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "packaging/windows"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def verifier():
    return load_script("verify_package")


@pytest.fixture
def package_content(verifier):
    content = {
        "config/agent.default.json": (SCRIPTS / "config/agent.default.json").read_bytes(),
        "install.bat": (SCRIPTS / "install.bat").read_bytes(),
        "uninstall.bat": b"offline uninstall",
        "diagnostics.bat": b"offline diagnostics",
        "requirements-offline.txt": b"pywin32==312\n",
        "src/opc_bridge/agent/client.py": (ROOT / "src/opc_bridge/agent/client.py").read_bytes(),
    }
    runtimes = {}
    for arch, directory, version, pin, tag, platform in (
        ("x64", "runtime", "3.14.6", "312", "cp314", "win_amd64"),
        ("x86", "runtime-x86", "3.8.10", "306", "cp38", "win32"),
    ):
        wheel = f"pywin32-{pin}-{tag}-{tag}-{platform}.whl"
        runtimes[arch] = {"path": directory, "architecture": arch, "python_version": version,
                          "pywin32_version": pin, "wheel": wheel}
        content[f"{directory}/python.exe"] = fake_pe(arch)
        for module in ("pythoncom", "pywintypes"):
            content[f"{directory}/Lib/site-packages/pywin32_system32/{module}{tag[2:]}.dll"] = fake_pe(arch)
        content[f"{directory}/Lib/site-packages/win32com/client/__init__.py"] = b"# stub"
        content[f"{directory}/Lib/site-packages/opc_bridge/adapters/da_worker.py"] = b"# stub"
        content[f"wheels/{arch}/{wheel}"] = b"test wheel placeholder"
        content[f"requirements-{arch}.txt"] = f"pywin32=={pin}\n".encode()
    content["runtime/pythonservice.exe"] = fake_pe("x64")
    manifest = {"version": "0.1.0", "commit": "a" * 40, "architecture": "x64",
                "runtime": "3.14.6", "worker_runtimes": runtimes,
                "sha256": verifier._tree_digest(content),
                "files": {path: hashlib.sha256(data).hexdigest() for path, data in content.items()}}
    content["manifest.json"] = json.dumps(manifest).encode()
    return content


def test_dual_runtime_manifest_is_accepted(monkeypatch, verifier, package_content):
    monkeypatch.setattr(verifier, "load_package_content", lambda path: (package_content, package_content))
    assert set(verifier.verify_package("memory")["worker_runtimes"]) == {"x64", "x86"}


@pytest.mark.parametrize("missing", ["runtime/python.exe", "runtime-x86/python.exe", "runtime/pythonservice.exe",
                                      "runtime-x86/Lib/site-packages/pywin32_system32/pythoncom38.dll"])
def test_both_runtimes_are_required(monkeypatch, verifier, package_content, missing):
    del package_content[missing]
    monkeypatch.setattr(verifier, "load_package_content", lambda path: (package_content, package_content))
    with pytest.raises(verifier.PackageVerificationError, match="missing required"):
        verifier.verify_package("memory")


def test_manifest_must_declare_both_runtimes(monkeypatch, verifier, package_content):
    manifest = json.loads(package_content["manifest.json"])
    del manifest["worker_runtimes"]["x86"]
    package_content["manifest.json"] = json.dumps(manifest).encode()
    monkeypatch.setattr(verifier, "load_package_content", lambda path: (package_content, package_content))
    with pytest.raises(verifier.PackageVerificationError, match="both x64 and x86"):
        verifier.verify_package("memory")


def test_runtime_cannot_mislabel_architecture(monkeypatch, verifier, package_content):
    package_content["runtime-x86/python.exe"] = fake_pe("x64")
    monkeypatch.setattr(verifier, "load_package_content", lambda path: (package_content, package_content))
    with pytest.raises(verifier.PackageVerificationError, match="PE architecture"):
        verifier.verify_package("memory")


def test_builder_requires_explicit_offline_inputs():
    builder = load_script("build_package")
    with pytest.raises(ValueError, match="required"):
        builder.build_package("not-created")


@pytest.mark.parametrize("arch,version,pin", [("x64", "3.14.6", "312"), ("x86", "3.8.10", "306")])
def test_runtime_metadata_inspection_does_not_import_com(monkeypatch, tmp_path, arch, version, pin):
    builder = load_script("build_package")
    (tmp_path / "python.exe").write_bytes(fake_pe(arch))
    (tmp_path / "pythonservice.exe").write_bytes(fake_pe("x64"))

    def run(command, **kwargs):
        assert command[0] == str(tmp_path / "python.exe")
        assert "pythoncom" not in command[-1] and "win32com" not in command[-1]
        return SimpleNamespace(stdout=json.dumps({"architecture": arch, "python_version": version,
                                                 "pywin32_version": pin}))

    monkeypatch.setattr(builder.subprocess, "run", run)
    result = builder.inspect_runtime(tmp_path, arch)
    assert result["path"] == ("runtime" if arch == "x64" else "runtime-x86")
    assert result["wheel"].endswith("win_amd64.whl" if arch == "x64" else "win32.whl")


def test_installer_exposes_interactive_and_unattended_architecture():
    installer = (SCRIPTS / "install.bat").read_text().lower()
    assert 'if /i "%~1"=="--worker-architecture"' in installer
    assert 'if "!unattended!"=="1" goto invalid_worker_architecture' in installer
    assert 'set /p "worker_architecture=' in installer
    assert 'set "setup_args=--worker-architecture !worker_architecture!"' in installer
    assert 'xcopy "%~dp0runtime-x86"' in installer
    assert "where python" not in installer and "where py" not in installer
    assert "-m pip" not in installer


@pytest.mark.parametrize("architecture", ["auto", "x64", "x86"])
def test_setup_persists_requested_choice(monkeypatch, tmp_path, architecture):
    from opc_bridge.adapters import worker_runtime

    setup = load_script("setup_config")
    config = tmp_path / "agent.json"
    monkeypatch.setenv("OPC_AUTH_TOKEN", "simulated-credential")
    monkeypatch.setattr(worker_runtime, "select_worker_runtime", lambda requested, exe: ("x86", "unused"))
    monkeypatch.setattr(setup, "DEFAULT_LOG_PATH", str(tmp_path / "logs/agent.log"))
    monkeypatch.setattr(setup.logger, "addHandler", lambda handler: handler.close())
    monkeypatch.setattr(sys, "argv", ["setup_config.py", "--unattended", "--worker-architecture", architecture,
                                     "--config-file", str(config)])
    setup.main()
    assert json.loads(config.read_text())["worker_architecture"] == architecture


def test_setup_rejects_invalid_choice_before_writing(monkeypatch, tmp_path):
    setup = load_script("setup_config")
    config = tmp_path / "agent.json"
    monkeypatch.setattr(sys, "argv", ["setup_config.py", "--worker-architecture", "bad",
                                     "--config-file", str(config)])
    with pytest.raises(SystemExit):
        setup.main()
    assert not config.exists()


def test_missing_x86_wheel_is_rejected(monkeypatch, verifier, package_content):
    del package_content["wheels/x86/pywin32-306-cp38-cp38-win32.whl"]
    monkeypatch.setattr(verifier, "load_package_content", lambda path: (package_content, package_content))
    with pytest.raises(verifier.PackageVerificationError, match="missing required"):
        verifier.verify_package("memory")


def test_installer_validates_choice_before_stopping_service():
    installer = (SCRIPTS / "install.bat").read_text()
    assert installer.index("select_worker_runtime(sys.argv[1]") < installer.index("sc.exe stop OPCBridgeAgent")
