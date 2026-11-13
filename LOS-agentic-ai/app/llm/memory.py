"""
FREE MEMORY, read without a dependency -- for the router's low-memory guard.

The 15.6 GB reference box crashed when the model, the server and a test run
shared it (2026-10-06). Below the configured floor the copilot does not call the
model at all: the fast lane answers and asks one clarifying question instead.

Windows: GlobalMemoryStatusEx (ullAvailPhys). Linux: /proc/meminfo MemAvailable.
Anything else, or a failed read: None -- unknown is not "low", and the call goes
ahead (the timeout still bounds it).
"""

from __future__ import annotations

import sys

GB = 1024 ** 3


def free_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return status.ullAvailPhys / GB
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024 / GB
    except Exception:  # noqa: BLE001 - a guard that cannot read memory does not block
        return None
    return None


#: (checked at, model resident?) -- Ollama's /api/ps is read at most every few seconds
_RESIDENT: dict[str, tuple[float, bool]] = {}
_RESIDENT_TTL_SECONDS = 5.0


def _model_resident() -> bool:
    """Whether Ollama already holds the configured model in memory (GET /api/ps). Any failure: False."""
    import json
    import time
    import urllib.request

    try:
        from app.llm.config import ollama_host, ollama_model

        model, host = str(ollama_model() or ""), str(ollama_host() or "").rstrip("/")
    except Exception:  # noqa: BLE001
        return False
    if not model or not host:
        return False
    cached = _RESIDENT.get(model)
    if cached and time.monotonic() - cached[0] < _RESIDENT_TTL_SECONDS:
        return cached[1]
    try:
        with urllib.request.urlopen(f"{host}/api/ps", timeout=0.3) as response:
            loaded = {str(m.get("name") or m.get("model") or "") for m in (json.load(response).get("models") or [])}
        resident = model in loaded
    except Exception:  # noqa: BLE001 - unknown: the free-memory reading decides
        resident = False
    _RESIDENT[model] = (time.monotonic(), resident)
    return resident


def free_gb_for_model() -> float | None:
    """The free memory a model call must respect. A model ALREADY LOADED needs no new memory (2026-10-13: with
    Qwen resident and 1.2 GB free, every Qwen step skipped itself) -> None ("not low"); otherwise free_gb()."""
    return None if _model_resident() else free_gb()


__all__ = ["free_gb", "free_gb_for_model"]
