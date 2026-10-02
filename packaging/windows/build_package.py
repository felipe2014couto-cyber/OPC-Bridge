"""Stage an offline dual-runtime Windows package from explicit local inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_package import verify_package

RUNTIME_PINS = {"x64": "312", "x86": "306"}
X86_PYTHON_VERSION = "3.8.10"


def inspect_runtime(root: Path, architecture: str) -> dict[str, str]:
    """Ask the supplied interpreter for metadata without importing COM."""
    executable = root / "python.exe"
    if not executable.is_file():
        raise ValueError(f"Explicit {architecture} runtime must include python.exe")
    code = (
        "import json,sys,struct; from importlib.metadata import version; "
        "print(json.dumps({'python_version':sys.version.split()[0],"
        "'architecture':'x64' if struct.calcsize('P')==8 else 'x86',"
        "'pywin32_version':version('pywin32')}))"
    )
    result = subprocess.run([str(executable), "-I", "-B", "-c", code],
                            capture_output=True, text=True, check=True)
    metadata = json.loads(result.stdout)
    if metadata["architecture"] != architecture:
        raise ValueError(f"Supplied {architecture} runtime has the wrong architecture")
    if metadata["pywin32_version"] != RUNTIME_PINS[architecture]:
        raise ValueError(f"Supplied {architecture} runtime has an unsupported pywin32 version")
    if architecture == "x86" and metadata["python_version"] != X86_PYTHON_VERSION:
        raise ValueError("The x86 worker requires Python 3.8.10")
    if architecture == "x64" and not (root / "pythonservice.exe").is_file():
        raise ValueError("The x64 main runtime requires pythonservice.exe")
    tag = "cp" + "".join(metadata["python_version"].split(".")[:2])
    platform_tag = "win_amd64" if architecture == "x64" else "win32"
    metadata["wheel"] = f"pywin32-{RUNTIME_PINS[architecture]}-{tag}-{tag}-{platform_tag}.whl"
    metadata["path"] = "runtime" if architecture == "x64" else "runtime-x86"
    return metadata


def compute_file_inventory(pkg_dir: str) -> dict[str, str]:
    root = Path(pkg_dir)
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.name != "manifest.json"}


def compute_tree_digest(pkg_dir: str) -> str:
    root = Path(pkg_dir)
    digest = hashlib.sha256()
    for relative in sorted(compute_file_inventory(pkg_dir)):
        digest.update(relative.encode("utf-8"))
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()


def build_package(output_dir: str, runtime_dir: str | None = None,
                  runtime_x86_dir: str | None = None, wheels_dir: str | None = None,
                  create_zip: bool = True) -> tuple[str, dict]:
    """Build only from supplied local runtimes; never download or use host Python."""
    if not runtime_dir or not runtime_x86_dir or not wheels_dir:
        raise ValueError("--runtime-dir, --runtime-x86-dir and --wheels-dir are required")
    roots = {"x64": Path(runtime_dir).resolve(), "x86": Path(runtime_x86_dir).resolve()}
    metadata = {arch: inspect_runtime(root, arch) for arch, root in roots.items()}
    wheel_source = Path(wheels_dir).resolve()
    for runtime in metadata.values():
        if not (wheel_source / runtime["wheel"]).is_file():
            raise ValueError(f"Required offline wheel is missing: {runtime['wheel']}")
    target = Path(output_dir).resolve()
    if target.name.lower() == "dist":
        target /= "opc-bridge-agent-windows-offline"
    # Do not erase an existing artifact or stage inside an input runtime.
    if target.exists() or any(root == target or root in target.parents or target in root.parents
                              for root in (*roots.values(), wheel_source)):
        raise ValueError("Output must be a new directory separate from runtime and wheel inputs")
    base = Path(__file__).resolve().parents[2]
    commit = subprocess.check_output(["git", "-C", str(base), "rev-parse", "HEAD"], text=True).strip()
    target.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pdb", "opc_bridge")
    for arch, root in roots.items():
        destination = target / metadata[arch]["path"]
        shutil.copytree(root, destination, ignore=ignore)
        shutil.copytree(base / "src/opc_bridge", destination / "Lib/site-packages/opc_bridge",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(base / "src/opc_bridge", target / "src/opc_bridge",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    scripts = Path(__file__).resolve().parent
    for name in ("install.bat", "uninstall.bat", "run_foreground.bat", "setup_config.py",
                 "verify_package.py", "diagnostics.py", "diagnostics.bat", "README_WINDOWS.md"):
        shutil.copy2(scripts / name, target / name)
    shutil.copytree(scripts / "config", target / "config")
    for arch, runtime in metadata.items():
        wheel_dest = target / "wheels" / arch
        wheel_dest.mkdir(parents=True)
        shutil.copy2(wheel_source / runtime["wheel"], wheel_dest / runtime["wheel"])
        (target / f"requirements-{arch}.txt").write_text(
            f"pywin32=={runtime['pywin32_version']}\n", encoding="utf-8")
    (target / "requirements-offline.txt").write_text("pywin32==312\n", encoding="utf-8")
    manifest = {"name": "opc-bridge-agent", "version": "0.1.0", "commit": commit,
                "architecture": "x64", "runtime": metadata["x64"]["python_version"],
                "worker_runtimes": metadata, "sha256": compute_tree_digest(str(target)),
                "files": compute_file_inventory(str(target))}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    verify_package(str(target), architecture="x64")
    artifact = str(target)
    if create_zip:
        artifact += ".zip"
        with zipfile.ZipFile(artifact, "x", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(target).as_posix())
        verify_package(artifact, architecture="x64")
    return artifact, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", "--output", dest="output", default="dist")
    parser.add_argument("--runtime-dir", required=True, help="Prepared x64 main Python/pywin32 runtime")
    parser.add_argument("--runtime-x86-dir", required=True, help="Prepared Python 3.8.10 x86/pywin32 306")
    parser.add_argument("--wheels-dir", required=True, help="Local directory containing both pinned wheels")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args()
    artifact, _ = build_package(args.output, args.runtime_dir, args.runtime_x86_dir,
                                args.wheels_dir, not args.no_zip)
    print(f"Verified offline package: {artifact}")


if __name__ == "__main__":
    main()
