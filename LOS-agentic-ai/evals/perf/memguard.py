"""
RAM SAFETY FOR BENCHMARKS (Phase 3 step 1, 2026-10-06).

The box crashed today when tests ran beside the live stack. Every benchmark here
checks free memory before each heavy call and stops -- with a clear message, never
silently -- when it falls below the floor (default 2 GB, MEMGUARD_FLOOR_GB).
"""

from __future__ import annotations

import os

import psutil

GB = 1024 ** 3


class LowMemory(RuntimeError):
    pass


def floor_gb() -> float:
    try:
        return float(os.getenv("MEMGUARD_FLOOR_GB", "2"))
    except ValueError:
        return 2.0


def free_gb() -> float:
    return psutil.virtual_memory().available / GB


def check(where: str) -> float:
    free = free_gb()
    if free < floor_gb():
        raise LowMemory(f"STOPPED at {where}: free memory {free:.2f} GB is below the {floor_gb():.1f} GB floor.")
    return free


def groups(names: tuple[str, ...] = ("ollama", "python", "postgres", "uvicorn")) -> dict[str, dict[str, float]]:
    """Resident memory (GB) and process count per process-name group."""
    out: dict[str, dict[str, float]] = {}
    for proc in psutil.process_iter(["name", "memory_info"]):
        name = str(proc.info.get("name") or "").lower()
        key = next((n for n in names if n in name), None)
        mem = proc.info.get("memory_info")
        if key and mem:
            slot = out.setdefault(key, {"processes": 0, "rss_gb": 0.0})
            slot["processes"] += 1
            slot["rss_gb"] = round(slot["rss_gb"] + mem.rss / GB, 3)
    return out


__all__ = ["LowMemory", "check", "floor_gb", "free_gb", "groups"]
