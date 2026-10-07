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


__all__ = ["free_gb"]
