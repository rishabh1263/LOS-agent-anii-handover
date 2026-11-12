"""
Measure the marksheet service on the synthetic set: status, class and name against labels.json, and the time.

    python scripts/eval_marksheet.py dev        # or heldout
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _norm(name: str | None) -> str:
    return re.sub(r"[^A-Z ]", "", (name or "").upper()).strip()


def main() -> int:
    split = sys.argv[1] if len(sys.argv) > 1 else "dev"
    folder = ROOT / "samples" / "marksheet" / split
    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    from app.agents.marksheet import service
    from app.agents.passbook import engine

    engine.warm()
    ok_status = ok_class = ok_name = n_ms = 0
    times, wrong, slow = [], [], []
    by_status: dict[str, list[float]] = {}
    for file, want in sorted(labels.items()):
        data = (folder / file).read_bytes()
        t = time.perf_counter()
        try:
            r = service._analyse_sync(data, file, "eval")
            status, cls, name = r.status, r.school_class, r.name.value
        except service.Refused as refused:
            status, cls, name = f"REFUSED:{refused.code}", None, None
        ms = (time.perf_counter() - t) * 1000
        times.append(ms)
        slow.append((ms, file, getattr(r, 'timings_ms', {})))
        by_status.setdefault(want["status"], []).append(ms)
        good = status == want["status"]
        ok_status += good
        if want["status"] == "VERIFIED":
            n_ms += 1
            ok_class += cls == want["class"]
            ok_name += _norm(name) == _norm(want["name"])
            if not (good and cls == want["class"] and _norm(name) == _norm(want["name"])):
                wrong.append(f"{file}: want {want['status']}/{want['class']}/{want['name']} got {status}/{cls}/{name} "
                             f"{getattr(r, 'reasons', '')}")
        elif not good:
            wrong.append(f"{file}: want {want['status']} got {status} {getattr(r, 'reasons', '')}")
    times.sort()
    print(f"{split}: status {ok_status}/{len(labels)} | class {ok_class}/{n_ms} | name {ok_name}/{n_ms}")
    print(f"time ms: P50 {statistics.median(times):.0f}  P95 {times[int(0.95 * (len(times) - 1))]:.0f}  "
          f"max {times[-1]:.0f}")
    for status, ms in sorted(by_status.items()):
        print(f"   {status:14} n={len(ms):3}  P50 {statistics.median(ms):6.0f} ms  max {max(ms):6.0f} ms")
    for ms, file, t in sorted(slow, reverse=True)[:3]:
        print(f"   slowest {file}: {ms:.0f} ms {t}")
    for line in wrong:
        print("  WRONG", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
