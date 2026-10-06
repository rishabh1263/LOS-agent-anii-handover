"""
BOUNDED APPEND-ONLY AUDIT LOGS -- size-based rotation with retention.

Every JSONL audit file (applicant agent, issuer verification, risk decisions,
agent runs) appends through `append_line`. When a file passes
AUDIT_LOG_MAX_MB (default 20) it is renamed to `<name>.1` (older ones shift
to .2, .3 ...) and a new file starts; only AUDIT_LOG_KEEP (default 5) rotated
files are kept. Nothing is rewritten or reordered -- a rotated file is the old
file, whole -- so the trail stays reproducible while disk use stays bounded.
(Measured 2026-10-05: three files had grown to 22-68 MB with no limit.)
"""

from __future__ import annotations

import os
from pathlib import Path


def _max_bytes() -> int:
    try:
        return int(float(os.getenv("AUDIT_LOG_MAX_MB") or 20) * 1024 * 1024)
    except ValueError:
        return 20 * 1024 * 1024


def _keep() -> int:
    try:
        return max(1, int(os.getenv("AUDIT_LOG_KEEP") or 5))
    except ValueError:
        return 5


def rotate_if_needed(path: Path) -> bool:
    """Rotate `path` when it is over the limit. Caller holds the file's lock."""
    try:
        if not path.exists() or path.stat().st_size < _max_bytes():
            return False
    except OSError:
        return False
    keep = _keep()
    oldest = path.with_name(f"{path.name}.{keep}")
    if oldest.exists():
        oldest.unlink()
    for index in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        if older.exists():
            older.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))
    return True


def append_line(path: Path, line: str) -> None:
    """Append one JSONL line, rotating first when the file is full. Caller holds the lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_if_needed(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
