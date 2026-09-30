"""Script to build the offline Windows installation bundle for OPC-Bridge Agent (T8).

Collects Python source files, configuration templates, batch scripts,
and downloads/copies pinned offline wheels so target industrial machines
can be installed without internet connectivity or development tools.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile

PINNED_DEPENDENCIES = [
    "pywin32==312",
]


def build_package(output_dir: str, create_zip: bool = True) -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    pkg_dir = os.path.abspath(output_dir)

    print(f"Building OPC-Bridge offline package in: {pkg_dir}")
    if os.path.exists(pkg_dir):
        shutil.rmtree(pkg_dir)
    os.makedirs(pkg_dir, exist_ok=True)

    # 1. Copy source code
    src_src = os.path.join(base_dir, "src", "opc_bridge")
    dst_src = os.path.join(pkg_dir, "src", "opc_bridge")
    print(f"Copying source code: {src_src} -> {dst_src}")
    shutil.copytree(src_src, dst_src, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # 2. Copy packaging scripts & config
    script_dir = os.path.dirname(__file__)
    config_dir = os.path.join(pkg_dir, "config")
    os.makedirs(config_dir, exist_ok=True)

    cfg_src = os.path.join(script_dir, "config", "agent.default.json")
    if os.path.exists(cfg_src):
        shutil.copy(cfg_src, os.path.join(config_dir, "agent.default.json"))

    for bat in ["install.bat", "uninstall.bat", "run_foreground.bat"]:
        bat_src = os.path.join(script_dir, bat)
        if os.path.exists(bat_src):
            shutil.copy(bat_src, os.path.join(pkg_dir, bat))

    readme_src = os.path.join(script_dir, "README_WINDOWS.md")
    if os.path.exists(readme_src):
        shutil.copy(readme_src, os.path.join(pkg_dir, "README_WINDOWS.md"))

    # 3. Download/bundle offline wheels if pip is available
    wheels_dir = os.path.join(pkg_dir, "wheels")
    os.makedirs(wheels_dir, exist_ok=True)

    # Write requirements-offline.txt
    req_file = os.path.join(pkg_dir, "requirements-offline.txt")
    with open(req_file, "w", encoding="utf-8") as f:
        for dep in PINNED_DEPENDENCIES:
            f.write(f"{dep}\n")

    print(f"Downloading pinned wheels to {wheels_dir}...")
    try:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            wheels_dir,
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
        sha256 = hashlib.sha256()
        with open(zip_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                sha256.update(chunk)
        digest = sha256.hexdigest()
        print(f"Package created: {zip_path}")
        print(f"Package SHA-256: {digest}")
        return zip_path

    print(f"Package folder ready: {pkg_dir}")
    return pkg_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Build offline Windows deployment package")
    parser.add_argument(
        "--output",
        default=os.path.join("dist", "opc-bridge-agent-windows-offline"),
        help="Target output directory",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="Skip ZIP archive generation",
    )
    args = parser.parse_args()
    build_package(args.output, create_zip=not args.no_zip)


if __name__ == "__main__":
    main()
