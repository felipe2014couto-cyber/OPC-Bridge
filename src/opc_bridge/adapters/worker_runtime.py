"""Read-only selection of bundled OPC worker runtimes; no COM activation."""
from __future__ import annotations

import logging
import os
import struct
from pathlib import Path

from opc_bridge.adapters.da import automation_wrapper_registered

logger = logging.getLogger(__name__)


def validate_worker_architecture(value: object) -> str:
    if not isinstance(value, str) or value not in ("auto", "x64", "x86"):
        raise ValueError("worker_architecture must be exactly auto, x64 or x86")
    return value


def pe_architecture(data: bytes) -> str:
    """Check the PE machine field without executing the runtime."""
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("Bundled runtime is not a Windows PE executable")
    offset = struct.unpack_from("<I", data, 60)[0]
    if offset + 6 > len(data) or data[offset:offset + 4] != b"PE\0\0":
        raise ValueError("Bundled runtime has an invalid PE header")
    machine = struct.unpack_from("<H", data, offset + 4)[0]
    if machine not in (0x8664, 0x14C):
        raise ValueError("Bundled runtime must be Windows x64 or x86")
    return "x64" if machine == 0x8664 else "x86"


def select_worker_runtime(requested: str, main_executable: str) -> tuple[str, str]:
    """Prefer x64 then x86 in auto; explicit choices never fall back."""
    validate_worker_architecture(requested)
    candidates = ("x64", "x86") if requested == "auto" else (requested,)
    selected = next((arch for arch in candidates if automation_wrapper_registered(arch)), None)
    if selected is None:
        unavailable = "either x64 or x86" if requested == "auto" else requested
        raise ConnectionError(
            f"OPC Automation wrapper is not registered for {unavailable}; "
            "a fully registered compatible OPC Automation component is required."
        )
    runtime_dir = os.path.dirname(os.path.abspath(main_executable))
    if os.path.basename(runtime_dir).lower() != "runtime":
        raise RuntimeError("OPC worker selection requires the bundled x64 main runtime")
    path = (os.path.join(runtime_dir, "python.exe") if selected == "x64" else
            os.path.join(os.path.dirname(runtime_dir), "runtime-x86", "python.exe"))
    try:
        actual = pe_architecture(Path(path).read_bytes())
    except (OSError, ValueError):
        raise RuntimeError(f"Bundled {selected} OPC worker runtime is unavailable") from None
    if actual != selected:
        raise RuntimeError(f"Bundled {selected} OPC worker runtime has the wrong architecture")
    logger.info(
        "OPC worker architecture: requested=%s selected=%s; reason=registered wrapper in %s view",
        requested, selected, selected,
    )
    return selected, path
