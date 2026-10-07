"""
GRANDFATHERED SIGNATURE REPORT -- READ ONLY (Phase 3 step 5c).

    python -m scripts.report_signature_gaps [--limit 5000] [--out runs/signature_gaps.json]

The mandatory signature rule (LOS_SIGNATURE_MANDATORY) blocks only cases created on
or after readiness.signature_mandatory.activation_date. Every OLDER case is
grandfathered: it is never blocked, and is listed here instead when the applicant or
a co-applicant has no signature whose presence check is VERIFIED. With no activation
date set, every case is treated as older. This script WRITES NOTHING to the case
store; it only calls list_* / get_* reads.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def gaps(repository, limit: int) -> list[dict]:
    from app.agents.applicant import config, workflow

    activation, _ = config.signature_activation()
    rows = []
    for applicant in repository.list_applicants(limit=limit):
        for application in repository.list_applications(applicant.applicant_id):
            created = application.created_at
            if activation is not None and created is not None:
                if created.tzinfo is None:
                    from datetime import timezone

                    created = created.replace(tzinfo=timezone.utc)
                if created >= activation:
                    continue                 # covered by the rule, not grandfathered
            items = workflow.party_signature_items(application, repository.list_documents(application.case_id))
            if items:
                rows.append({"case_id": application.case_id, "applicant_id": applicant.applicant_id,
                             "created_at": str(application.created_at),
                             "gaps": [{"party_role": i.get("party_role", "PRIMARY_APPLICANT"), "code": i["code"],
                                       "detail": i["detail"]} for i in items]})
    return rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=5000, help="applicants to scan (newest first)")
    p.add_argument("--out", default=str(ROOT / "runs" / "signature_gaps.json"))
    args = p.parse_args()

    from app.store import get_repository

    rows = gaps(get_repository(), args.limit)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"cases": rows, "count": len(rows)}, indent=2), encoding="utf-8")
    print(f"{len(rows)} grandfathered case(s) without a verified signature for every party -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
