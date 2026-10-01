"""Validate the contents and security properties of an offline Windows ZIP."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile


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


def verify_package(zip_path: str, architecture: str | None = None) -> dict[str, object]:
    """Validate a package ZIP and return its manifest."""
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        _fail(f"cannot open package ZIP: {exc}")

    with archive:
        members = {name.replace("\\", "/"): name for name in archive.namelist()}
        if len(members) != len(archive.namelist()):
            _fail("package contains duplicate paths")
        if "manifest.json" not in members:
            _fail("missing manifest.json")
        content = {path: archive.read(name) for path, name in members.items()}
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
        wheel_prefix = f"wheels/"
        wheel_names = [path.rsplit("/", 1)[-1].lower() for path in members if path.startswith(wheel_prefix)]
        for dependency in dependency_lines:
            package = re.split(r"[<>=!~;\[]", dependency, maxsplit=1)[0].replace("_", "-")
            if not any(name.startswith(package.replace("-", "_")) or name.startswith(package)
                       for name in wheel_names):
                _fail(f"no bundled wheel found for dependency {package}")

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
        if not config["auth_token"]:
            _fail("bootstrap config must contain a non-empty token")
        if not config["certfile"]:
            _fail("bootstrap config must specify a CA certificate")
        ca_path = str(config["certfile"]).replace("\\", "/")
        if ca_path not in members:
            _fail(f"bootstrap CA certificate is missing: {ca_path}")

        installer = content["install.bat"].decode("utf-8", errors="replace").lower()
        for forbidden in ("where python", "where py", "python.exe was not found", "system python"):
            if forbidden in installer:
                _fail("installer can fall back to Python installed on the machine")
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

        actual_sha256 = _tree_digest(content)
        if manifest["sha256"].lower() != actual_sha256:
            _fail("manifest sha256 does not match packaged files")
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify an offline Windows agent ZIP")
    parser.add_argument("zip_path")
    parser.add_argument("--architecture", choices=("x86", "x64"))
    args = parser.parse_args()
    try:
        manifest = verify_package(args.zip_path, args.architecture)
    except PackageVerificationError as exc:
        parser.error(str(exc))
    print(f"Package verified: {manifest['version']} ({manifest['architecture']})")


if __name__ == "__main__":
    main()
