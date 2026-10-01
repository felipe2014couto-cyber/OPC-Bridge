"""Script to build the offline Windows installation bundle for OPC-Bridge Agent (T8).

Collects Python source files, configuration templates, batch scripts,
and downloads/copies pinned offline wheels so target industrial machines
can be installed without internet connectivity or development tools.
"""
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

from verify_package import verify_package

PINNED_DEPENDENCIES = [
    "pywin32==312",
]


def _tree_sha256(root: str) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def build_package(
    output_dir: str,
    create_zip: bool = True,
    architecture: str = "x64",
    central_address: str = "127.0.0.1:8443",
    agent_id: str = "opc-agent-windows-01",
    token: str = "",
    ca_file: str = "",
) -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    norm = os.path.normpath(output_dir)
    if os.path.basename(norm).lower() == "dist":
        pkg_dir = os.path.abspath(os.path.join(norm, "opc-bridge-agent-windows-offline"))
    else:
        pkg_dir = os.path.abspath(output_dir)

    print(f"Building OPC-Bridge offline package in: {pkg_dir}")
    if os.path.exists(pkg_dir):
        shutil.rmtree(pkg_dir)
    os.makedirs(pkg_dir, exist_ok=True)

    if architecture not in {"x86", "x64"}:
        raise ValueError("architecture must be x86 or x64")
    if create_zip and (not token or not ca_file or not os.path.isfile(ca_file)):
        raise ValueError("a bootstrap token and existing CA certificate file are required")
    host, _, port = central_address.rpartition(":")
    if create_zip and (not host or not port.isdigit()):
        raise ValueError("central_address must be HOST:PORT")

    # Embed the selected architecture's official embeddable runtime. The caller
    # provides an extracted Python distribution and matching pip bootstrap.
    runtime_source = os.environ.get("OPC_PYTHON_RUNTIME")
    runtime_dst = os.path.join(pkg_dir, "runtime")
    if runtime_source and os.path.isdir(runtime_source):
        shutil.copytree(runtime_source, runtime_dst)

    # 1. Copy source code
    src_src = os.path.join(base_dir, "src", "opc_bridge")
    dst_src = os.path.join(pkg_dir, "src", "opc_bridge")
    print(f"Copying source code: {src_src} -> {dst_src}")
    shutil.copytree(src_src, dst_src, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 2. Copy packaging scripts & config
    script_dir = os.path.dirname(__file__)
    config_dir = os.path.join(pkg_dir, "config")
    os.makedirs(config_dir, exist_ok=True)

    ca_name = os.path.basename(ca_file) if ca_file else "ca.pem"
    if ca_file and os.path.isfile(ca_file):
        shutil.copy2(ca_file, os.path.join(config_dir, ca_name))
    config = {
        "server_host": host,
        "server_port": int(port),
        "agent_id": agent_id,
        "auth_token": token,
        "opc_prog_id": "",
        "update_rate_ms": 1000,
        "certfile": f"config/{ca_name}",
        "log_file": r"C:\ProgramData\OPCBridge\logs\agent.log",
        "log_level": "INFO",
    }
    with open(os.path.join(config_dir, "agent.default.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    for bat in ["install.bat", "uninstall.bat", "run_foreground.bat", "setup_config.py"]:
        bat_src = os.path.join(script_dir, bat)
        if os.path.exists(bat_src):
            shutil.copy(bat_src, os.path.join(pkg_dir, bat))

    readme_src = os.path.join(script_dir, "README_WINDOWS.md")
    if os.path.exists(readme_src):
        shutil.copy(readme_src, os.path.join(pkg_dir, "README_WINDOWS.md"))

    # 3. Download/bundle offline wheels for the selected Windows architecture.
    wheels_dir = os.path.join(pkg_dir, "wheels")
    os.makedirs(wheels_dir, exist_ok=True)

    # Write requirements-offline.txt
    req_file = os.path.join(pkg_dir, "requirements-offline.txt")
    with open(req_file, "w", encoding="utf-8") as f:
        for dep in PINNED_DEPENDENCIES:
            f.write(f"{dep}\n")

    print(f"Downloading pinned wheels to {wheels_dir}...")
    try:
        python_exe = os.path.join(runtime_dst, "python.exe")
        pip_dir = os.path.join(runtime_dst, "Scripts")
        os.makedirs(pip_dir, exist_ok=True)
        cmd = [
            python_exe,
            "-m",
            "pip",
            "download",
            "--dest",
            wheels_dir,
            "--platform",
            "win32" if architecture == "x86" else "win_amd64",
            "--python-version",
            "38",
            "--implementation",
            "cp",
            "--abi",
            "cp38",
            "--only-binary=:all:",
            "-r",
            req_file,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            print("Successfully downloaded offline wheels.")
        else:
            print(f"Pip download note: {res.stderr.strip()}")
    except Exception as exc:
        print(f"Could not download wheels automatically ({exc}); target machine can use existing wheels.")

    if not create_zip:
        return pkg_dir
    if not os.path.isfile(os.path.join(runtime_dst, "python.exe")):
        raise RuntimeError("OPC_PYTHON_RUNTIME must provide runtime/python.exe")
    if not os.listdir(wheels_dir):
        raise RuntimeError("no compatible offline wheels were downloaded")

    manifest = {
        "version": "0.1.0",
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=base_dir, capture_output=True, text=True, check=True
        ).stdout.strip(),
        "architecture": architecture,
        "runtime": "CPython 3.8 Windows embeddable",
    }
    with open(os.path.join(pkg_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    manifest["sha256"] = _tree_sha256(pkg_dir)
    with open(os.path.join(pkg_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # 4. Create ZIP archive if requested
    if create_zip:
        zip_path = f"{pkg_dir}.zip"
        print(f"Creating ZIP archive: {zip_path}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(pkg_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, pkg_dir)
                    zf.write(full_path, rel_path)
        verify_package(zip_path, architecture)
        with open(zip_path, "rb") as package_file:
            digest = hashlib.sha256(package_file.read()).hexdigest()
        print(f"Package created: {zip_path}")
        print(f"Package SHA-256: {digest}")
        return zip_path

    print(f"Package folder ready: {pkg_dir}")
    return pkg_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Build offline Windows deployment package")
    parser.add_argument(
        "--output-dir",
        "--output",
        dest="output",
        default=os.path.join("dist", "opc-bridge-agent-windows-offline"),
        help="Target output directory",
    )
    parser.add_argument("--architecture", choices=("x86", "x64"), default="x64")
    parser.add_argument("--central-address", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Skip ZIP archive generation",
    )
    args = parser.parse_args()
    build_package(
        args.output,
        create_zip=not args.no_zip,
        architecture=args.architecture,
        central_address=args.central_address,
        agent_id=args.agent_id,
        token=args.token,
        ca_file=args.ca_file,
    )


if __name__ == "__main__":
    main()
