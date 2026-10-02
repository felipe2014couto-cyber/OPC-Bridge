"""Report worker selection and bundled runtime metadata without activating COM."""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

from opc_bridge.adapters.worker_runtime import select_worker_runtime


def collect_diagnostics(output_dir: str = r"C:\ProgramData\OPCBridge\logs") -> tuple[str, bool]:
    lines = ["OPC-Bridge worker diagnostics (no COM activation)", "Main service architecture: x64"]
    root = Path(__file__).resolve().parent
    manifest = root / "manifest.json"
    if manifest.is_file():
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        lines.append("Worker runtimes: " + json.dumps(metadata.get("worker_runtimes", {})))
    config_path = Path(r"C:\ProgramData\OPCBridge\agent.json")
    ok = False
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        requested = config.get("worker_architecture", "auto")
        try:
            selected, _ = select_worker_runtime(requested, sys.executable)
            lines.extend([f"Requested worker architecture: {requested}",
                          f"Selected worker architecture: {selected}",
                          "Reason: fully registered wrapper in selected registry view"])
            ok = True
        except (ValueError, RuntimeError, ConnectionError):
            lines.append("Worker selection failed: invalid choice, registration or bundled runtime")
    else:
        lines.append("Agent configuration is unavailable")
    # Include only known architecture decision messages, not general logs/configuration.
    for name in ("install.log", "agent.log"):
        path = Path(output_dir) / name
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
                if "OPC worker architecture: requested=" in line:
                    lines.append(line)
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    report = Path(output_dir) / f"diagnostics-{stamp}.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(report), ok


def main() -> None:
    report, ok = collect_diagnostics()
    print(f"Worker diagnostics: {'PASS' if ok else 'FAIL'}; report={report}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
