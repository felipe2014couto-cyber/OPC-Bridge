"""Validate the contents and security properties of an offline Windows ZIP or extracted package."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

from opc_bridge.adapters.worker_runtime import pe_architecture, validate_worker_architecture


class PackageVerificationError(ValueError):
    """Raised when an offline package does not meet its distribution contract."""


def _fail(message: str) -> None:
    raise PackageVerificationError(message)


def _tree_digest(content: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, data in sorted(content.items()):
        if path != "manifest.json":
            digest.update(path.encode("utf-8"))
            digest.update(data)
    return digest.hexdigest()


def load_package_content(target_path: str) -> tuple[dict[str, str], dict[str, bytes]]:
    """Load members and file content from either a ZIP file or an extracted directory."""
    if os.path.isdir(target_path):
        members = {}
        content = {}
        root = Path(target_path)
        for p in root.rglob("*"):
            if p.is_file():
                if "__pycache__" in p.parts or p.name.endswith(".pyc"):
                    continue
                rel_posix = p.relative_to(root).as_posix()
                members[rel_posix] = str(p)
                content[rel_posix] = p.read_bytes()
        return members, content

    try:
        archive = zipfile.ZipFile(target_path)
    except (OSError, zipfile.BadZipFile) as exc:
        _fail(f"cannot open package ZIP: {exc}")

    with archive:
        raw_names = archive.namelist()
        members = {
            name.replace("\\", "/"): name
            for name in raw_names
            if "__pycache__" not in name and not name.endswith(".pyc")
        }
        if len(members) != len([n for n in raw_names if "__pycache__" not in n and not n.endswith(".pyc")]):
            _fail("package contains duplicate paths")
        content = {path: archive.read(name) for path, name in members.items()}
        return members, content


def verify_package(target_path: str, architecture: str | None = None) -> dict[str, object]:
    """Validate a package ZIP or extracted directory and return its manifest."""
    members, content = load_package_content(target_path)

    if "manifest.json" not in members:
        _fail("missing manifest.json")

    try:
        manifest = json.loads(content["manifest.json"])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _fail(f"invalid manifest.json: {exc}")

    for field in ("version", "commit", "architecture", "runtime", "sha256"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            _fail(f"manifest field {field!r} is required")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", manifest["commit"]):
        _fail("manifest commit must be a Git SHA")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", manifest["sha256"]):
        _fail("manifest sha256 must contain 64 hexadecimal characters")
    if architecture and manifest["architecture"].lower() != architecture.lower():
        _fail(f"architecture mismatch: expected {architecture}, got {manifest['architecture']}")

    def require(path: str) -> None:
        if path not in members:
            _fail(f"missing required package file: {path}")

    require("runtime/python.exe")
    require("config/agent.default.json")
    require("install.bat")
    require("uninstall.bat")
    require("diagnostics.bat")
    require("requirements-offline.txt")

    agent_sources = [
        path for path in members
        if path.startswith("src/opc_bridge/agent/") and path.endswith(".py")
    ]
    if not agent_sources:
        _fail("package contains no agent Python source files")

    dependency_lines = [
        line.strip().lower()
        for line in content["requirements-offline.txt"].decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not dependency_lines:
        _fail("requirements-offline.txt contains no dependencies")

    wheel_prefix = "wheels/"
    wheel_names = [path.rsplit("/", 1)[-1].lower() for path in members if path.startswith(wheel_prefix)]
    for dependency in dependency_lines:
        package = re.split(r"[<>=!~;\[]", dependency, maxsplit=1)[0].replace("_", "-")
        if not any(name.startswith((package.replace("-", "_"), package))
                   for name in wheel_names):
            _fail(f"no bundled wheel found for dependency {package}")

    if manifest["architecture"] != "x64":
        _fail("main service architecture must be x64")
    runtimes = manifest.get("worker_runtimes")
    if not isinstance(runtimes, dict) or set(runtimes) != {"x64", "x86"}:
        _fail("manifest must declare both x64 and x86 worker runtimes")
    for arch, root, platform_tag, pin in (
        ("x64", "runtime", "win_amd64", "312"),
        ("x86", "runtime-x86", "win32", "306"),
    ):
        metadata = runtimes[arch]
        if not isinstance(metadata, dict) or metadata.get("path") != root:
            _fail(f"invalid {arch} worker runtime metadata")
        if metadata.get("architecture") != arch or metadata.get("pywin32_version") != pin:
            _fail(f"invalid {arch} worker runtime architecture or pywin32 version")
        python_version = metadata.get("python_version", "")
        if not isinstance(python_version, str) or not re.fullmatch(r"3\.\d+\.\d+", python_version):
            _fail(f"invalid {arch} Python version")
        if arch == "x86" and python_version != "3.8.10":
            _fail("x86 worker requires Python 3.8.10")
        tag = "cp" + "".join(python_version.split(".")[:2])
        wheel = f"pywin32-{pin}-{tag}-{tag}-{platform_tag}.whl"
        if metadata.get("wheel") != wheel:
            _fail(f"invalid {arch} wheel metadata")
        require(f"wheels/{arch}/{wheel}")
        require(f"requirements-{arch}.txt")
        if content[f"requirements-{arch}.txt"].decode().strip() != f"pywin32=={pin}":
            _fail(f"invalid {arch} offline dependency pin")
        require(f"{root}/python.exe")
        if pe_architecture(content[f"{root}/python.exe"]) != arch:
            _fail(f"incorrect {arch} runtime PE architecture")
        if arch == "x64":
            require(f"{root}/pythonservice.exe")
            if pe_architecture(content[f"{root}/pythonservice.exe"]) != arch:
                _fail("main service executable must be x64")
        for module in ("pythoncom", "pywintypes"):
            suffix = "".join(python_version.split(".")[:2])
            dll = f"{root}/Lib/site-packages/pywin32_system32/{module}{suffix}.dll"
            require(dll)
            if pe_architecture(content[dll]) != arch:
                _fail(f"incorrect {arch} {module} DLL architecture")
        require(f"{root}/Lib/site-packages/opc_bridge/adapters/da_worker.py")
        require(f"{root}/Lib/site-packages/win32com/client/__init__.py")

    expected_arch = manifest["architecture"].lower()
    pywin_files = [name for name in wheel_names if name.startswith("pywin32")]
    if not pywin_files:
        _fail("missing bundled pywin32 wheel")
    arch_markers = {"x86": ("win32", "i386", "x86"), "x64": ("win_amd64", "amd64", "x64")}
    markers = arch_markers.get(expected_arch)
    if markers is None or not any(any(marker in name for marker in markers) for name in pywin_files):
        _fail(f"no pywin32 wheel matching architecture {manifest['architecture']}")
    if not any(path.startswith("runtime/Lib/site-packages/") and "pywin32" in path.lower()
               for path in members):
        _fail("runtime is missing installed pywin32 files")

    try:
        config = json.loads(content["config/agent.default.json"])
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _fail(f"invalid bootstrap config: {exc}")
    for key in ("server_host", "server_port", "agent_id", "auth_token", "certfile"):
        if key not in config:
            _fail(f"bootstrap config is missing {key!r}")
    if not config["server_host"] or not config["agent_id"]:
        _fail("bootstrap config must specify central address and agent identity")
    # Credentials and the CA may be provided securely during installation.
    if not isinstance(config["auth_token"], str):
        _fail("bootstrap token must be a string")
    validate_worker_architecture(config.get("worker_architecture"))
    if config["certfile"]:
        ca_path = str(config["certfile"]).replace("\\", "/")
        if ca_path not in members:
            _fail("bootstrap CA certificate is missing")

    installer = content["install.bat"].decode("utf-8", errors="replace").lower()
    for forbidden in ("where python", "where py", "python.exe was not found", "system python"):
        if forbidden in installer:
            _fail("installer can fall back to Python installed on the machine")
    if "--worker-architecture" not in installer or "runtime-x86\\python.exe" not in installer:
        _fail("installer must expose worker selection and require both runtimes")
    if "runtime\\python.exe" not in installer and "runtime/python.exe" not in installer:
        _fail("installer does not invoke the bundled Python runtime")

    source_text = "\n".join(
        content[path].decode("utf-8", errors="replace")
        for path in members
        if path.startswith("src/opc_bridge/agent/") and path.endswith(".py")
    )
    if "create_default_context(cafile=self._certfile or None)" not in source_text:
        _fail("agent TLS does not configure CA-based certificate validation")
    if "CERT_NONE" in source_text or "check_hostname = False" in source_text:
        _fail("agent TLS disables certificate validation")
    if "ssl" not in source_text.lower():
        _fail("agent source does not contain TLS configuration")

    # Verify inventory hashes if manifest contains file-level inventory
    file_inventory = manifest.get("files")
    if not isinstance(file_inventory, dict) or not file_inventory:
        _fail("manifest must include a complete file inventory")
    if isinstance(file_inventory, dict):
        if set(file_inventory) != set(content) - {"manifest.json"}:
            _fail("manifest inventory must cover every packaged file")
        for file_path, expected_hash in file_inventory.items():
            norm_path = file_path.replace("\\", "/")
            if norm_path not in content:
                _fail(f"inventory file {file_path!r} missing from package")
            file_hash = hashlib.sha256(content[norm_path]).hexdigest()
            if file_hash.lower() != expected_hash.lower():
                _fail(f"hash mismatch for inventory file {file_path!r}")

    actual_sha256 = _tree_digest(content)
    if manifest["sha256"].lower() != actual_sha256:
        _fail("manifest sha256 does not match packaged files")

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify an offline Windows agent ZIP or extracted package")
    parser.add_argument("target_path", help="Path to package ZIP file or extracted package folder")
    parser.add_argument("--architecture", choices=("x86", "x64"), default="x64")
    args = parser.parse_args()
    try:
        manifest = verify_package(args.target_path, args.architecture)
    except PackageVerificationError as exc:
        parser.error(str(exc))
    print(f"Package verified successfully: {manifest['version']} ({manifest['architecture']})")


if __name__ == "__main__":
    main()
