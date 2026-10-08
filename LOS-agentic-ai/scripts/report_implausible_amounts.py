"""
READ-ONLY REPORT (FOS plan 1.1): every case whose recorded amount is below its plausible minimum
(applicant_agent.yaml chatbot.plausibility.minimum), or missing. Nothing is changed.

    python -m scripts.report_implausible_amounts            # uses LOS_STORE_DSN / the dev store, like the service
"""

from __future__ import annotations

from dotenv import load_dotenv


def main() -> int:
    load_dotenv()
    from app.agents.applicant import config
    from app.store import get_repository

    minimum = (config.chatbot("plausibility") or {}).get("minimum") or {}
    repository = get_repository()
    rows = repository._all("SELECT case_id, applicant_id, product, loan_amount, created_at FROM applications "
                           "ORDER BY created_at", ())
    flagged = []
    for row in rows:
        row = dict(row)
        amount = row.get("loan_amount")
        try:
            value = float(str(amount).replace(",", "")) if amount not in (None, "") else None
        except ValueError:
            value = None
        floor = minimum.get("loan_amount")
        if value is None:
            flagged.append((row, "MISSING"))
        elif floor is not None and value < float(floor):
            flagged.append((row, f"BELOW_MINIMUM (< {floor})"))
    print(f"{len(rows)} cases checked, {len(flagged)} flagged (loan_amount minimum {minimum.get('loan_amount')})")
    for row, why in flagged:
        print(f"  {row['case_id']}  {row.get('product') or '--':<14} loan_amount={row.get('loan_amount')!s:<10} {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
