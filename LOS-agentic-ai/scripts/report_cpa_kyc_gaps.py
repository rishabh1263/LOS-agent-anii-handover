"""
GRANDFATHERED CASES REPORT -- READ ONLY (Phase 3 step 2).

    python -m scripts.report_cpa_kyc_gaps [--limit 5000] [--out runs/cpa_kyc_gaps.json]

Lists every case already at CPA or a later stage whose KYC would NOT pass the new
FOS -> CPA rule (every `cpa_gate: true` check PASSED for every party). Those cases
are grandfathered: nothing is moved back, and this script WRITES NOTHING to the
case store -- it only calls list_* / get_* reads. The output is for a person to
review.

The rule is evaluated whether or not LOS_FOS_CPA_KYC_RULE is on, so the impact can
be seen before the flag is switched on.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def gaps(repository, limit: int) -> list[dict]:
    from app.agents.los import kyc_gate, stages

    rows = []
    for applicant in repository.list_applicants(limit=limit):
        for application in repository.list_applications(applicant.applicant_id):
            case_id = application.case_id
            stage = stages.resolve(case_id).stage
            if stage is None or stage is stages.LosStage.FOS:
                continue
            result = kyc_gate.evaluate(case_id, repository)
            if result["status"] == "PASS":
                continue
            rows.append({
                "case_id": case_id, "applicant_id": applicant.applicant_id, "stage": stage.value,
                "kyc_gate": result["status"],
                "failing": [{"party_role": p["party_role"], "check": c["check"], "status": c["status"],
                             "reason": c["reason"]}
                            for p in result["parties"] for c in p["checks"] if c["status"] != "PASS"],
            })
    return rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=5000, help="applicants to scan (newest first)")
    p.add_argument("--out", default=str(ROOT / "runs" / "cpa_kyc_gaps.json"))
    args = p.parse_args()

    from app.store import get_repository

    rows = gaps(get_repository(), args.limit)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"cases": rows, "count": len(rows)}, indent=2), encoding="utf-8")
    print(f"{len(rows)} case(s) at CPA or later whose KYC would not pass the new rule -> {out}")
    for r in rows[:50]:
        print(f"  {r['case_id']}  {r['stage']:<12} {r['kyc_gate']:<9} "
              + ", ".join(f"{f['party_role'].lower()}:{f['check']}={f['status']}" for f in r["failing"][:4]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
