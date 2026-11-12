"""
The credit underwriting UNDERSTANDING WORKBOOK -- the repayment rules, where every figure comes from, how it connects,
and what could be added -- built from config/credit_underwriting (so it never drifts from what the code runs).

    python scripts/make_credit_rules_workbook.py [out.xlsx]      # default: ~/Downloads/Credit_Underwriting_Rules.xlsx

The "Demo results" sheet runs the 35 SYNTHETIC demo cases (samples/credit_bank_reports); no customer data is read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

HEAD = PatternFill("solid", fgColor="1F3864")
BAND = PatternFill("solid", fgColor="EEF2F8")
FILLS = {"PASS": "C6EFCE", "RECOMMENDED": "C6EFCE", "REFER": "FFEB9C", "DECLINE": "FFC7CE",
         "NOT_RECOMMENDED": "FFC7CE", "HIGH": "FFC7CE", "MEDIUM": "FFEB9C", "LOW": "DDEBF7"}
THIN = Border(*(Side(style="thin", color="BFBFBF"),) * 4)
WRAP = Alignment(wrap_text=True, vertical="top")


def sheet(wb: Workbook, title: str, header: list[str], rows: list[list], widths: list[int], *,
          note: str | None = None, color_cols: tuple[int, ...] = ()) -> None:
    ws = wb.create_sheet(title)
    r = 1
    if note:
        ws.cell(1, 1, note).font = Font(italic=True, color="595959")
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(header))
        ws.cell(1, 1).alignment = WRAP
        ws.row_dimensions[1].height = 45
        r = 2
    for j, h in enumerate(header, 1):
        c = ws.cell(r, j, h)
        c.font, c.fill, c.alignment, c.border = Font(bold=True, color="FFFFFF"), HEAD, WRAP, THIN
    for i, row in enumerate(rows, r + 1):
        for j, v in enumerate(row, 1):
            c = ws.cell(i, j, v)
            c.alignment, c.border = WRAP, THIN
            if (i - r) % 2 == 0:
                c.fill = BAND
            if j in color_cols and str(v) in FILLS:
                c.fill = PatternFill("solid", fgColor=FILLS[str(v)])
                c.font = Font(bold=True)
    for j, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = ws.cell(r + 1, 1)


def overview(wb: Workbook, pol: dict) -> None:
    ws = wb.active
    ws.title = "Overview"
    lines = [
        ("CREDIT UNDERWRITING -- REPAYMENT REPORT", None),
        ("One question", "Can this applicant repay the proposed EMI? -- answered the way a CPA / credit officer reads "
                         "a bank statement, from the vendor's VERIFIED bank analysis + the case's eligibility data."),
        ("Input (no OCR)", "Vendor (Leviosa) bank analysis, 6-12 months: the .xlsx workbook or normalized .json. Its "
                           "fields stay the vendor's (sheet + field names); PAN / e-mail / mobile / address / account "
                           "number are dropped (account kept as XXXX1234)."),
        ("Our brain", "1) Obligation map (karz ka naksha)  2) Declared vs bank  3) 5 rules  4) Vendor vs our check  "
                      "5) Eligibility cap  6) Suggestion (EMI / loan that fits)."),
        ("Verdict", "Any rule DECLINE -> NOT_RECOMMENDED;  else any REFER -> REFER;  else RECOMMENDED.  Then the "
                    "eligibility decision caps it (sheet 'Eligibility cap'). A figure that could not be measured "
                    "is never PASS: the rule REFERs and says what is missing."),
        ("Output", "verdict + can_repay (YES / NEEDS_REVIEW / NO) + headline + reason points + 5 rules (each with the "
                   "vendor fields it read) + 7 sections + suggestion. An assessment for the officer, NOT a credit "
                   "decision."),
        ("Flow", "ONE API: POST /api/v1/credit/assess/{case_id} (vendor file optional) -> store -> report -> "
                 "credit agent  |  Credit agent (orchestrator 'credit_agent') -> MCP 'repayment.get' "
                 "(app/mcp/credit.py; server app/mcp/credit_server.py) -> signal repayment.verdict -> policy rule "
                 "UW_BANK_REPAYMENT_* -> finding -> assessment (READY_FOR_DECISION / REVIEW_REQUIRED) -> memo."),
        ("Config (all of it)", "app/config/credit_underwriting/  policy.yaml (rules, limits, eligibility cap, "
                               "observations)  |  vendor_fields.yaml (vendor sheet / field map, sources, cross "
                               "checks)  |  settings.yaml (store, upload, scopes)  |  wording.yaml (every sentence the officer reads).  Credit agent: "
                               "underwriting_policy.yaml (evidence_plan step 'repayment', UW_BANK_REPAYMENT_* rules)."),
        ("Policy status", f"version {pol.get('version')} -- signed_off: {pol.get('signed_off')}  (DEMO limits until "
                          "the credit team signs them off; the report says so)"),
        ("Sheets", "5 Rules | Docs Policy | Eligibility cap | Observations | Vendor vs ours | API response | Vendor field map | "
                   "Can add (ideas) | Demo results | Where to change"),
    ]
    for i, (k, v) in enumerate(lines, 1):
        a = ws.cell(i, 1, k)
        a.font = Font(bold=True, size=14 if v is None else 11, color="1F3864")
        if v is not None:
            b = ws.cell(i, 2, v)
            b.alignment = WRAP
            a.alignment = WRAP
            ws.row_dimensions[i].height = max(30, 15 * (len(v) // 95 + 1))
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 22, 120


# plain words for each rule: what the CPA checks, why it matters
RULE_TEXT = {
    "DATA_COMPLETE": ("Kya bank data poora hai aur account applicant ka hi hai?",
                      "6 full months; opening + transactions = closing every month; account holder name = applicant",
                      "Adhoore ya toote-phoote data par faisla galat hota hai; kisi aur ka account dikhana fraud ka "
                      "pehla tareeka hai."),
    "REPAYMENT_TRACK": ("Kya har EMI / cheque time pe gaya?",
                        "inward EMI / cheque / ECS bounces + months an ongoing EMI did not go out",
                        "Pichla repayment behaviour hi agle repayment ka sabse accha signal hai."),
    "TRUE_FOIR": ("Bank me dikhi SAARI EMIs + nayi EMI, income ka kitna %?",
                  "(max(declared EMI, EMIs seen in the bank) + proposed EMI) / declared monthly income",
                  "Applicant chhupi hui EMI nahi batata; bank sab dikhata hai. Eligibility FOIR declared EMI par "
                  "hai, yeh 'true' FOIR hai."),
    "BALANCE_COVER": ("Kya account me EMI dene layak paisa rehta hai?",
                      "ABB = average of the balance on days 5/10/15/20/25 (window months) / proposed EMI",
                      "Income aata hai par turant nikal jaata hai toh EMI date pe bounce hoga."),
    "INCOME_SUPPORT": ("Kya declared income sach me bank me aata hai?",
                       "median monthly genuine inflow (credits minus loans taken, own transfers, reversals) / "
                       "declared income",
                       "Declared income badha-chadha ke batana sabse common gap hai."),
}


def _limits(code: str, r: dict) -> tuple[str, str]:
    """The rule's REFER / DECLINE limits in plain words (values from policy.yaml)."""
    g = r.get
    text = {
        "DATA_COMPLETE": (f"fewer than {g('min_full_months')} full months, or more than {g('max_balance_breaks')} "
                          "month(s) whose balances do not add up" +
                          (", or the account holder is not the applicant" if g("holder_must_match") else ""),
                          "never -- REFER only (more data or the right account fixes it)"),
        "REPAYMENT_TRACK": (f"more than {g('refer_bounces_window')} bounce or more than {g('refer_missed_emis')} "
                            "missed EMI in the window",
                            f"more than {g('decline_bounces_window')} bounces or more than "
                            f"{g('decline_missed_emis')} missed EMIs"),
        "TRUE_FOIR": (f"above {g('refer_above_pct')}% of declared income",
                      f"above {g('decline_above_pct')}%"),
        "BALANCE_COVER": (f"ABB (days {', '.join(map(str, g('eod_days') or []))}) below "
                          f"{g('refer_below_times_emi')} x the new EMI",
                          f"below {g('decline_below_times_emi')} x the new EMI"),
        "INCOME_SUPPORT": (f"bank inflow below {g('refer_below_pct')}% of declared income",
                           f"below {g('decline_below_pct')}%"),
    }
    return text.get(code, ("; ".join(f"{k}={v}" for k, v in r.items() if k.startswith("refer")),
                           "; ".join(f"{k}={v}" for k, v in r.items() if k.startswith("decline"))))


def rules_sheet(wb: Workbook, pol: dict, vend: dict) -> None:
    rows = []
    for i, (code, r) in enumerate(pol["rules"].items(), 1):
        q, how, why = RULE_TEXT.get(code, ("", "", ""))
        refer, decline = _limits(code, r)
        rows.append([i, code, r["label"], q, how, "\n".join(vend.get("sources", {}).get(code, [])),
                     refer, decline, why])
    sheet(wb, "5 Rules", ["#", "Rule", "Label", "Kya dekhta hai (CPA ka sawaal)", "Kaise naapte hain",
                          "Vendor sheet / fields used", "REFER when (policy.yaml)", "DECLINE when (policy.yaml)",
                          "Kyu zaroori hai"],
          rows, [4, 18, 28, 32, 45, 40, 34, 28, 42],
          note=f"Window: last {pol['window_months']} FULL months (a part month is never averaged). Not measurable "
               "-> REFER, never PASS. Values from app/config/credit_underwriting/policy.yaml.")


def gate_sheet(wb: Workbook, pol: dict) -> None:
    meaning = {"APPROVED": "eligible as asked", "CONDITIONAL": "eligible for a lower amount (that EMI is checked)",
               "REFER": "eligibility itself needs a review", "DATA_MISSING": "mandatory eligibility data missing",
               "REJECTED": "fails eligibility policy"}
    rows = [[d, meaning.get(d, ""), v] for d, v in (pol.get("eligibility_gate") or {}).items()]
    sheet(wb, "Eligibility cap", ["Eligibility decision", "Meaning", "Best verdict the bank report may give"],
          rows, [22, 50, 34], color_cols=(3,),
          note="A case eligibility rejected is never 'recommended' because its bank account looks fine; a bad bank "
               "is never lifted by eligibility. Final verdict = the WORSE of the two.")


def observations_sheet(wb: Workbook, pol: dict) -> None:
    o = pol["observations"]
    text = {"new_loans_months": ("Naye loan liye", "loans disbursed in the last N months", "loan stacking"),
            "cash_deposit_share_pct": ("Cash heavy credits", "cash deposits > N% of credits", "income source unclear"),
            "top_payer_share_pct": ("Ek hi payer", "one counterparty > N% of inflow", "concentration / dependency"),
            "spike_times_median": ("Ek mahine ka spike", "a month's inflow > N x median month",
                                   "window dressing / one-off"),
            "falling_balance_pct": ("Girta balance", "ABB of last 3 months N% below the first 3",
                                    "stress building up")}
    rows = [[text.get(k, (k,))[0], k, v, *text.get(k, ("", "", ""))[1:]] for k, v in o.items()]
    sheet(wb, "Observations", ["Point", "policy.yaml key", "Limit", "When shown", "Why the officer cares"], rows,
          [22, 26, 10, 42, 34], note="NOT rules: shown to the officer as reason points, never deciding on their own.")


def crosscheck_sheet(wb: Workbook, vend: dict) -> None:
    ours = {"foir_pct": "EMIs in our obligation map / median monthly inflow",
            "emi_payments": "sum of EMIs in our obligation map over the statement",
            "fixed_obligations": "(shown in the vendor report section)",
            "cheque_bounces_6m": "inward EMI / cheque bounces in our window",
            "emi_detected": "any loan in our obligation map", "cheque_bounce_flag": "our bounce count > 0",
            "monthly_average_balance": "our ABB on policy days (different basis: guide only, never judged)"}
    rows = [[k, v[0], v[1], ours.get(k, "")] for k, v in vend["cross_check"].items() if isinstance(v, list)]
    sheet(wb, "Vendor vs ours", ["Check", "Vendor sheet", "Vendor field", "Our figure"], rows, [26, 32, 30, 55],
          note=f"Apart by more than {vend['cross_check'].get('differ_pct')}% -> a reason point 'vendor says X, our "
               "check found Y -- verify'. Real report: the vendor's EMI total covered only its own EMI sheet; our "
               "map found an ECS loan it missed.")


# what each block of the answer is for (the field list comes from the contract itself)
BLOCKS = {
    "request_id": "Quote it to support; it is in the logs.",
    "case_id": "The case asked about.",
    "decision": "RECOMMENDED / REFER / NOT_RECOMMENDED.",
    "can_repay": "YES / NEEDS_REVIEW / NO -- the answer in one word.",
    "summary": "The answer in one plain line.",
    "emi": "The new EMI, the most EMI the income carries, the loan that EMI repays, a note.",
    "report": "THE VENDOR REPORT, section by section in the Excel's order (account, month by month, daily balances, "
              "money in, money out, loans, bounces, vendor's totals): one plain line, key figures, flags.",
    "insights": "OURS, not in the vendor report: EMI dry run and money left each month (one line each).",
    "checks": "The 5 checks: label, pass / review / fail, value, limit.",
    "problems": "What is wrong, in plain words (left out when nothing is).",
    "watch": "Points to look at; they never decide on their own.",
    "next_steps": "What the officer should do next, one line per open point.",
    "working": "The figures behind the answer, one formula per line (income, EMIs, FOIR, balance).",
    "people": "Only with a co-applicant: whose statement arrived, and what it shows.",
    "loans": "Every loan seen in the bank (karz ka naksha): EMI, status, found by vendor or our check.",
    "underwriting": "The credit agent: ran or not (and why), status, one line, findings needing review.",
    "note": "Reminder: this helps the officer; it is not the loan decision.",
    "detail": "Everything else (detail=true): months, top payers / payees, dry-run months, check findings and "
              "vendor fields, vendor vs our figures, people in full, policy, run id, internal sections.",
    "ai_summary": "LAST: a senior officer's 3-4 sentence note by the local AI (checked), else built from the facts.",
}


def response_sheet(wb: Workbook) -> None:
    from app.agents.credit.repayment.api_response import AssessResponse

    rows = []
    for i, (name, field) in enumerate(AssessResponse.model_fields.items(), 1):
        model = getattr(field.annotation, "model_fields", None)
        args = getattr(field.annotation, "__args__", ()) or ()
        inner = next((getattr(a, "model_fields", None) for a in args if getattr(a, "model_fields", None)), None)
        fields = model or inner or {}
        rows.append([i, name, BLOCKS.get(name, ""), ", ".join(fields)])
    sheet(wb, "API response", ["#", "Block", "What it is for", "Fields"], rows, [4, 16, 70, 70],
          note="POST /api/v1/credit/assess/{case_id} -- form fields: file (optional .xlsx/.json), party "
               "(applicant / co_applicant), co_applicant_id, run_underwriting (default true), applicant_name, detail, "
               "explain (AI summary, default true). Empty fields are left out. Errors: {code, message, request_id} "
               "in plain words (400 / 401 / 403 / 404 / 413 / 415 / 422 / 503).")


def field_map_sheet(wb: Workbook, vend: dict) -> None:
    rows = [[vend["sheets"]["account"], k, v, "account"] for k, v in vend["account"].items()]
    for ours, parts in vend["monthly"].items():
        for part, key in parts.items():
            rows.append([vend["sheets"]["monthly"], key, f"{ours}.{part}", "monthly"])
    rows.append([vend["sheets"]["monthly"], "sBal<N>", "eod[N]", "monthly (balance on day N)"])
    rows += [[vend["sheets"]["transactions"], k, v, "transactions"] for k, v in vend["transactions"].items()]
    rows += [[name, "(all columns, vendor names)", "vendor_view", "kept as sent"]
             for name in vend["vendor_view"]["tables"] + vend["vendor_view"]["key_value"]]
    sheet(wb, "Vendor field map", ["Vendor sheet", "Vendor field", "Our name (internal)", "Block"], rows,
          [32, 30, 30, 26], note="20 vendor sheets -> 4 blocks + the vendor's own view; Derived Analysis, Monthwise "
                                 "Credits and Debits, Monthwise EOD Balances and AccountXns are subsets, kept once.")


IDEAS = [
    ("EMI-date balance", "Balance on the EMI debit day, just before the EMI", "AllAccountXns sBalance + Emi "
     "Transactions sDate", "HIGH", "Bounce ka sabse seedha predictor: ABB theek par EMI wale din khaali account.",
     "6th rule or replace BALANCE_COVER days with EMI day"),
    ("Negative / zero balance days", "Days the balance was <= 0", "Monthly Details sBalMin, AllAccountXns sBalance",
     "HIGH", "OD / overdrawn account = cash stress", "REFER above N days"),
    ("Penal / bounce charges", "Bank penal charges in the window", "Overall Analysis 'Penal charges', "
     "sCategory Bank_fees", "MEDIUM", "Bounce charges reveal bounces the vendor did not label", "observation"),
    ("Loan stacking", ">= 2 new loans in 3 months", "sTotalLoanDisbursal, sCategory Loan_Disbursed", "HIGH",
     "Ek loan se dusra chukana (debt spiral)", "rule (REFER) or observation"),
    ("Salary regularity", "Salary credited on a steady day, steady amount", "SalaryXns, sSalaries, sTotalSalary",
     "MEDIUM", "Salaried: job stability; a missing month = job change / delay", "observation; salaried only"),
    ("Salary payer vs declared employer", "Salary payer name vs employer on the application", "SalaryXns / "
     "counter_party", "MEDIUM", "Declared employer real hai ya nahi", "Declared vs bank row"),
    ("Inflow volatility", "Coefficient of variation of monthly inflow", "Monthly Details sTotalCredit", "MEDIUM",
     "Self-employed: unstable business income", "observation; SE only"),
    ("Circular / round-tripping", "Same amount in and out within 1-2 days, same party", "AllAccountXns",
     "HIGH", "Turnover badha ke dikhana (inflated banking)", "REFER"),
    ("Month-end window dressing", "Balance spike in the last days before the statement ends", "sBal25..sBal31",
     "MEDIUM", "Statement se pehle paisa daal ke ABB badhana", "observation"),
    ("Cash withdrawal share", "Cash / ATM withdrawals as % of debits", "sTotalCashWithdrawal, sTotalAtmWithdrawal",
     "LOW", "Cash spending: hard to trace obligations", "observation"),
    ("Credit card minimum-due pattern", "Card payments = min due month after month", "sTotalCreditCardPayment",
     "MEDIUM", "Revolving debt / stress", "observation"),
    ("Gambling / crypto / betting", "Debits to such merchants", "sCategory / counter_party", "MEDIUM",
     "Behavioural risk", "observation (word list in config)"),
    ("Bureau vs bank EMI", "Bureau total monthly obligation vs our bank EMI map", "bureau.get + obligation map",
     "HIGH", "Loans paid from another account / not in bureau", "cross check in the credit agent"),
]


def ideas_sheet(wb: Workbook) -> None:
    sheet(wb, "Can add (ideas)", ["Idea", "What it measures", "Vendor data available", "Value", "Why (CPA view)",
                                  "Suggested as"], [list(i) for i in IDEAS], [26, 40, 38, 9, 44, 30],
          color_cols=(4,), note="Not built. Each is config + one function when the credit team asks for it; "
                                "keep the 5 rules small and add only what changes a decision.")


def demo_sheet(wb: Workbook) -> None:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from app.agents.credit.repayment import service

    service._case_store_co_applicants = lambda case_id: []   # demo cases are not LOS cases (and the case store
    #                                                           may be held by the running server)
    from app.agents.eligibility.data import dev_db

    dev_db.ensure()
    rows = []
    for f in sorted((ROOT / "samples" / "credit_bank_reports").glob("*.json")):
        story = json.loads(f.read_text(encoding="utf-8")).get("story")
        try:
            r = service.report_for(f.stem)
        except service.RepaymentError as exc:
            rows.append([f.stem, story, "", exc.code, "", "", "", "", "", "", ""])
            continue
        res = {o["rule"]: o["result"] for o in r["rules"]}
        rows.append([f.stem, story, r["eligibility"]["decision"], r["verdict"],
                     *[res.get(k) for k in ("DATA_COMPLETE", "REPAYMENT_TRACK", "TRUE_FOIR", "BALANCE_COVER",
                                            "INCOME_SUPPORT")],
                     r["headline"], "\n".join(r["reasons"][:3])])
    sheet(wb, "Demo results", ["Case (synthetic)", "Story", "Eligibility", "Verdict", "DATA_COMPLETE",
                               "REPAYMENT_TRACK", "TRUE_FOIR", "BALANCE_COVER", "INCOME_SUPPORT", "Headline",
                               "Top reasons"], rows,
          [20, 13, 14, 18, 15, 17, 12, 15, 16, 45, 70], color_cols=(4, 5, 6, 7, 8, 9),
          note="35 SYNTHETIC demo cases (scripts/make_demo_bank_reports.py), every verdict path shown. No customer "
               "data.")


# ---- section: Docs Policy -- the Credit Document Policy Agent's 11 rules (app/agents/docs_verify_credit_agent) ------
DOCS = ROOT / "app" / "agents" / "docs_verify_credit_agent"
# which outcome kinds (policy `critical_when`) a rule can raise -- the rest of what it finds is "to collect"
DOCS_KINDS = {"RULE-02": ("MISSING", "REJECTED", "MISMATCH"), "RULE-03": ("MISSING", "REJECTED", "MISMATCH"),
              "RULE-04": ("MISSING", "REJECTED", "MISMATCH"), "RULE-05": ("NOT_ACCEPTED",),
              "RULE-09": ("EXCEPTION_PENDING",)}


def _docs_policies() -> list[tuple[str, dict]]:
    out = []
    for f in sorted((DOCS / "policies").glob("*.credit.json")):
        p = json.loads(f.read_text(encoding="utf-8"))
        if f.name != "example_policy.json":
            out.append((", ".join(p["applies_to"]["products"]), p))
    return out


def _docs_value(rule_id: str, p: dict) -> str:
    """The policy's own setting for a rule, in plain words (from the product's policies/*.credit.json)."""
    names = {k.upper(): v for k, v in p.get("display_names", {}).items() if not k.startswith("_")}
    words = lambda t: names.get(str(t).upper()) or str(t).replace("_", " ").lower()  # noqa: E731
    if p.get("rules", {}).get(rule_id) is False:
        return "switched OFF in this policy"
    if rule_id == "RULE-01":
        return f"product checklist ({p['base']['source']}) through the {p['base']['through_stage']} stage"
    if rule_id == "RULE-02":
        co = p["parties"].get("CO_APPLICANT", {})
        return ("co-applicant: own checklist on their own employment" +
                (f"; not asked: {', '.join(words(x) for x in co.get('exclude', []))}" if co.get("exclude") else ""))
    if rule_id == "RULE-03":
        return ("a missing required document blocks (critical); ask the customer"
                if "MISSING" in (p.get("critical_when") or []) else "a missing document is listed to collect")
    if rule_id == "RULE-04":
        return ("an unfilled detail BLOCKS" if p.get("unevaluated_conditions_block") else
                "an unfilled detail (e.g. employment) is listed, does not block")
    if rule_id == "RULE-05":
        return "; ".join(f"{words(k)}: {', '.join(words(x) for x in v)} NOT accepted"
                         for k, v in p.get("same_kind", {}).items() if not k.startswith("_")) or "-"
    if rule_id == "RULE-06":
        lines = []
        for r in p.get("period_and_freshness", []):
            what = ", ".join(words(t) for t in r["document_types"])
            bits = ([f"{r['min_months']} months"] if r.get("min_months") else
                    ["months from the product checklist"] if r.get("min_months_from") else []) + \
                   ([f"within {r['max_age_days']} days"] if r.get("max_age_days") is not None else []) + \
                   (["not expired"] if r.get("expiry_field") or r.get("expiry_from") else [])
            lines.append(f"{what}: {', '.join(bits)}")
        return "\n".join(lines)
    if rule_id == "RULE-07":
        return "\n".join(f"{words(r['when_present'])} needs {', '.join(words(x) for x in r['requires_one_of'])}"
                         for r in p.get("supporting_documents", [])) or "-"
    if rule_id == "RULE-08":
        cs = p.get("cross_stage", {})
        return (f"reused from {', '.join(cs.get('reuse_from_stages', []))}; collect again at credit: "
                f"{', '.join(words(x) for x in cs.get('collect_at_stage', [])) or 'none'}")
    if rule_id == "RULE-09":
        return "\n".join(f"{', '.join(words(s) for s in e['slots'])} <- {', '.join(e['deviation_codes'])} (APPROVED only)"
                         for e in p.get("exceptions", [])) or "-"
    if rule_id == "RULE-10":
        return "always on (cannot be switched off)"
    if rule_id == "RULE-11":
        fr = p.get("file_rules") or {}
        d = fr.get("default", {})
        lines = [f"default: {'/'.join(d.get('formats', []))}, {d.get('min_kb')} KB - {d.get('max_mb')} MB"]
        for t, lim in fr.get("by_type", {}).items():
            lines.append(f"{words(t)}: " + ", ".join(f"{k}={'/'.join(v) if isinstance(v, list) else v}"
                                                      for k, v in lim.items()))
        return "\n".join(lines)
    return "-"


def _docs_critical(rule_id: str, p: dict) -> str:
    crit = set(p.get("critical_when") or [])
    if rule_id == "RULE-06":
        return "Critical" if any(r.get("blocking") for r in p.get("period_and_freshness", [])) else "To collect"
    if rule_id == "RULE-07":
        return "Critical" if any(r.get("blocking") for r in p.get("supporting_documents", [])) else "To collect"
    if rule_id == "RULE-11":
        return "Critical" if (p.get("file_rules") or {}).get("blocking") else "To collect"
    if rule_id in ("RULE-01", "RULE-10"):
        return "Critical (policy / audit failure)"
    kinds = [k for k in DOCS_KINDS.get(rule_id, ()) if k in crit]
    return f"Critical when {', '.join(k.replace('_', ' ').lower() for k in kinds)}" if kinds else "To collect"


def docs_policy_sheet(wb: Workbook) -> None:
    import yaml

    board = yaml.safe_load((DOCS / "config" / "dashboard.yaml").read_text(encoding="utf-8"))
    names = yaml.safe_load((DOCS / "config" / "reasons.yaml").read_text(encoding="utf-8"))["rule_names"]
    policies = _docs_policies()
    rows, n = [], 0
    for sec in board["sections"]:
        for rule_id in sec["rules"]:
            n += 1
            first = policies[0][1]
            rows.append([n, sec["name"], rule_id, names[rule_id], board["rule_help"].get(rule_id, ""),
                         board["rule_explain"].get(rule_id, ""), _docs_critical(rule_id, first),
                         *[_docs_value(rule_id, p) for _, p in policies]])
    header = ["#", "Section", "Rule", "Name", "Kya dekhta hai", "Kaise (plain words)", "Critical / To collect",
              *[f"{prod} ({p['version']})" for prod, p in policies]]
    sheet(wb, "Docs Policy", header, rows, [4, 20, 9, 24, 32, 70, 26, *[34] * len(policies)],
          note="CREDIT DOCUMENT POLICY AGENT -- is every document on file and usable for credit underwriting? "
               "POST /api/v1/credit/document-readiness/{case_id}. Critical (missing, not an accepted type, rejected, "
               "mismatch / possibly fake, exception awaiting approval) -> refer to the credit manager (ACM / RCM); "
               "everything else is 'to collect' and the case moves ahead with notes. Built from "
               "app/agents/docs_verify_credit_agent: config/dashboard.yaml (sections, plain words), "
               "config/reasons.yaml (rule names), policies/*.credit.json (values). All policies are SAMPLE.")
    ws = wb["Docs Policy"]
    for row in ws.iter_rows(min_row=3, min_col=7, max_col=7):
        for c in row:
            if str(c.value).startswith("Critical"):
                c.fill, c.font = PatternFill("solid", fgColor=FILLS["DECLINE"]), Font(bold=True)
            elif c.value == "To collect":
                c.fill, c.font = PatternFill("solid", fgColor=FILLS["REFER"]), Font(bold=True)


def where_sheet(wb: Workbook) -> None:
    rows = [
        ["Change a limit (e.g. FOIR 50 -> 55)", "app/config/credit_underwriting/policy.yaml", "rules.TRUE_FOIR"],
        ["Sign off the policy", "policy.yaml", "signed_off: true + version"],
        ["Eligibility cap", "policy.yaml", "eligibility_gate"],
        ["Observation limits", "policy.yaml", "observations"],
        ["EMI recognition (tolerance, lender words)", "policy.yaml", "obligations"],
        ["Vendor renames a field / new vendor", "vendor_fields.yaml", "account / monthly / transactions maps"],
        ["Which vendor sheets are kept / quoted", "vendor_fields.yaml", "vendor_view"],
        ["Fees that are never an EMI", "vendor_fields.yaml", "categories.not_emi"],
        ["Vendor vs ours pairs, tolerance", "vendor_fields.yaml", "cross_check"],
        ["Change any sentence the officer reads", "wording.yaml", "headline / rules / limits / watch / suggestion / underwriting"],
        ["Store: files / postgres", "settings.yaml", "store.backend"],
        ["Who may upload / read", "settings.yaml", "scopes"],
        ["Credit agent reads the report or not", "app/config/underwriting_policy.yaml",
         "evidence_plan 'repayment' + UW_BANK_REPAYMENT_* rules"],
        ["Docs Policy: a limit (days, months, file size), Critical kinds, rule on / off",
         "app/agents/docs_verify_credit_agent/policies/<product>.credit.json",
         "period_and_freshness / file_rules / critical_when / rules"],
        ["Docs Policy: which documents a product needs", "app/config/policies/<product>.yaml + stage_requirements.yaml",
         "(the existing product checklist)"],
        ["Docs Policy: exception -> document mapping", "policies/<product>.credit.json", "exceptions"],
        ["Docs Policy: any sentence, rule name, error", "app/agents/docs_verify_credit_agent/config/reasons.yaml",
         "rule_names / parties / reasons / errors"],
        ["Docs Policy: route words, AI note, summary", "app/agents/docs_verify_credit_agent/config/wording.yaml",
         "route / next_step / ai_summary"],
        ["Docs Policy: dashboard sections, plain words", "app/agents/docs_verify_credit_agent/config/dashboard.yaml",
         "sections / rule_help / rule_explain / labels"],
    ]
    sheet(wb, "Where to change", ["I want to...", "File", "Key"], rows, [44, 46, 48],
          note="Nothing is hard-coded: every limit, word list, field name and label is in config.")


def main() -> int:
    from app.agents.credit.repayment import config

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Downloads" / "Credit_Underwriting_Rules.xlsx"
    pol, vend, settings = config.policy(), config.vendor(), config.settings()
    wb = Workbook()
    overview(wb, pol)
    rules_sheet(wb, pol, vend)
    docs_policy_sheet(wb)
    gate_sheet(wb, pol)
    observations_sheet(wb, pol)
    crosscheck_sheet(wb, vend)
    response_sheet(wb)
    field_map_sheet(wb, vend)
    ideas_sheet(wb)
    demo_sheet(wb)
    where_sheet(wb)
    wb.save(out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
