"""
PASSBOOK VERDICT -- independent checks over the extracted fields and the IFSC master. Deterministic; no LLM.

    VERIFIED       every check this service defines passed
    REJECTED       a decisive contradiction: the IFSC belongs to a different bank than the one printed
    MANUAL_REVIEW  anything ambiguous, incomplete, masked, low-confidence or not confirmable against the master
    UNREADABLE     not enough legible text to say anything
    UNSUPPORTED    not a passbook (another document, or nothing recognisable as one)
    ERROR          an internal failure (set by the service, never here)

WHAT VERIFIED MEANS, AND DOES NOT. The page reads as a passbook; the account number, IFSC and holder name are legible
and well-formed; the IFSC exists in the RBI-derived master; the printed bank and branch agree with it. It is NOT
proof that the account exists today, that it belongs to the uploader, or that the page was never edited -- no live
bank source is consulted. Tampering detection is limited to what is implemented and tested: conflicting values for
one field on the same page (AMBIGUOUS) and a bank / IFSC contradiction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.agents.passbook import extract as X
from app.agents.passbook import ifsc as IFSC

VERIFIED, REJECTED, MANUAL_REVIEW, UNREADABLE, UNSUPPORTED, ERROR = (
    "VERIFIED", "REJECTED", "MANUAL_REVIEW", "UNREADABLE", "UNSUPPORTED", "ERROR")


@dataclass
class Check:
    name: str
    result: str                  # PASS | FAIL | REVIEW | NOT_APPLICABLE
    reason: str | None = None
    detail: str | None = None


@dataclass
class Verdict:
    status: str
    reasons: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)


def classify(text: str, cfg: dict) -> tuple[str | None, float]:
    """(other document type or None, passbook score). Captions only -- no field is trusted to decide this."""
    upper = re.sub(r"\s+", " ", (text or "").upper())
    compact = re.sub(r"[^A-Z/ ]", "", upper)
    for marker, other in (cfg.get("other_documents") or {}).items():
        if marker in compact and "PASSBOOK" not in compact.replace(" ", ""):
            return other, 0.0
    score = sum(w for marker, w in (cfg.get("markers") or {}).items() if marker in compact)
    return None, round(min(score, 1.0), 3)


def _similar(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    na, nb = re.sub(r"[^A-Z0-9 ]", " ", a.upper()).split(), re.sub(r"[^A-Z0-9 ]", " ", b.upper()).split()
    ja, jb = "".join(na), "".join(nb)
    if ja and jb and (ja in jb or jb in ja):
        return 1.0
    return SequenceMatcher(None, ja, jb).ratio()


def _merged(ifsc: str | None, vcfg: dict) -> str | None:
    """The bank a merged-away bank's IFSC prefix now belongs to (passbook.yaml verification.merged_banks), or None."""
    return (vcfg.get("merged_banks") or {}).get((ifsc or "")[:4]) if ifsc else None


def decide(fields: dict[str, X.Field], *, other_document: str | None, passbook_score: float, readable: bool,
           cfg: dict) -> Verdict:
    """The verdict and every check behind it. `fields` holds the five public fields plus `printed_bank` and
    `printed_branch` (internal)."""
    checks: list[Check] = []
    reasons: list[str] = []
    vcfg = cfg["verification"]
    min_conf = float(vcfg["min_ocr_confidence"])

    def add(name, result, reason=None, detail=None):
        checks.append(Check(name, result, reason, detail))
        if reason and reason not in reasons:
            reasons.append(reason)

    # 1-3: is this a readable passbook at all
    if other_document:
        add("is_passbook", "FAIL", "NOT_A_PASSBOOK", f"reads as {other_document}")
        return Verdict(UNSUPPORTED, reasons, checks)
    if not readable:
        add("readable", "FAIL", "UNREADABLE", "no legible text")
        return Verdict(UNREADABLE, reasons, checks)
    account, ifsc_f, holder = fields["account_number"], fields["ifsc_code"], fields["account_holder_name"]
    legible_core = sum(f.status in (X.PRESENT, X.MASKED) for f in (account, ifsc_f, holder))
    is_passbook = passbook_score >= float(cfg["classification"]["min_score"])
    if not is_passbook and legible_core < 2:
        add("is_passbook", "FAIL", "NOT_A_PASSBOOK", f"passbook score {passbook_score}")
        return Verdict(UNSUPPORTED, reasons, checks)
    add("is_passbook", "PASS" if is_passbook else "REVIEW", None if is_passbook else "PASSBOOK_CAPTIONS_WEAK",
        f"score {passbook_score}")
    if legible_core == 0:
        add("readable", "FAIL", "KEY_FIELDS_UNREADABLE")
        return Verdict(UNREADABLE, reasons, checks)
    add("readable", "PASS")

    # 4-7: account, IFSC, master, bank and branch consistency
    status_reason = {X.MISSING: "ACCOUNT_NUMBER_MISSING", X.MASKED: "ACCOUNT_NUMBER_MASKED",
                     X.UNREADABLE: "ACCOUNT_NUMBER_UNREADABLE", X.INVALID: "ACCOUNT_NUMBER_INVALID",
                     X.AMBIGUOUS: "ACCOUNT_NUMBER_CONFLICT"}
    if account.status == X.PRESENT:
        low = account.confidence < min_conf
        add("account_number", "REVIEW" if low else "PASS", "ACCOUNT_NUMBER_LOW_CONFIDENCE" if low else None)
    else:
        add("account_number", "REVIEW", status_reason.get(account.status, "ACCOUNT_NUMBER_UNREADABLE"),
            "; ".join(account.notes) or None)

    branch = None
    if ifsc_f.status == X.PRESENT:
        branch = IFSC.lookup(ifsc_f.value)
        if not IFSC.available():
            add("ifsc_in_master", "REVIEW", "IFSC_MASTER_UNAVAILABLE")
        elif branch is None and _merged(ifsc_f.value, vcfg):
            # a bank merged away (Andhra -> Union, 2020): its old IFSCs left the RBI list, its passbooks did not
            old, new = ifsc_f.value[:4], _merged(ifsc_f.value, vcfg)
            low = ifsc_f.confidence < min_conf
            add("ifsc_in_master", "REVIEW" if low else "PASS", "IFSC_LOW_CONFIDENCE" if low else None,
                f"pre-merger IFSC: {IFSC.bank_name(old) or old} merged into {IFSC.bank_name(new) or new}")
        elif branch is None:
            add("ifsc_in_master", "REVIEW", "IFSC_NOT_IN_MASTER")
        else:
            low = ifsc_f.confidence < min_conf
            add("ifsc_in_master", "REVIEW" if low else "PASS", "IFSC_LOW_CONFIDENCE" if low else None,
                f"{branch.bank} / {branch.branch}")
    else:
        add("ifsc_in_master", "REVIEW", {X.MISSING: "IFSC_MISSING", X.AMBIGUOUS: "IFSC_CONFLICT"}.get(
            ifsc_f.status, "IFSC_INVALID"))

    printed_bank = fields["printed_bank"]
    printed_code = X.code_of(printed_bank)
    if branch and printed_code and printed_code != branch.bank_code:
        add("bank_matches_ifsc", "FAIL", "BANK_IFSC_CONFLICT",
            f"printed {printed_bank.value}, IFSC belongs to {branch.bank}")
    elif branch and printed_code:
        add("bank_matches_ifsc", "PASS")
    elif branch:
        add("bank_matches_ifsc", "NOT_APPLICABLE", None, "bank name not printed / not read; the IFSC names it")
    elif ifsc_f.status == X.PRESENT and _merged(ifsc_f.value, vcfg) and printed_code:
        old, new = ifsc_f.value[:4], _merged(ifsc_f.value, vcfg)
        if printed_code in (old, new):
            add("bank_matches_ifsc", "PASS", None, f"printed {printed_bank.value}; the IFSC is {old}'s")
        else:
            add("bank_matches_ifsc", "FAIL", "BANK_IFSC_CONFLICT",
                f"printed {printed_bank.value}, IFSC belongs to {IFSC.bank_name(old) or old}")
    elif ifsc_f.status == X.PRESENT and _merged(ifsc_f.value, vcfg):
        add("bank_matches_ifsc", "NOT_APPLICABLE", None, "bank name not printed / not read; the IFSC names it")
    elif printed_code:
        add("bank_matches_ifsc", "REVIEW", "BANK_NOT_CONFIRMED", "no IFSC to confirm the printed bank")
    else:
        add("bank_matches_ifsc", "REVIEW", "BANK_NOT_IDENTIFIED")

    if account.status == X.PRESENT and branch:
        lengths = (cfg["account_number"].get("lengths") or {}).get(branch.bank_code)
        if lengths and len(account.value) not in lengths:
            add("account_format_for_bank", "REVIEW", "ACCOUNT_NUMBER_FORMAT_UNUSUAL",
                f"{len(account.value)} digits; {branch.bank_code} uses {sorted(lengths)}")
        else:
            add("account_format_for_bank", "PASS" if lengths else "NOT_APPLICABLE")

    printed_branch = fields["printed_branch"]
    if branch and printed_branch.status == X.PRESENT:
        sim = _similar(printed_branch.value, branch.branch)
        ok = sim >= float(vcfg["branch_match_min_similarity"])
        add("branch_matches_ifsc", "PASS" if ok else "REVIEW", None if ok else "BRANCH_IFSC_MISMATCH",
            f"similarity {sim:.2f}")
    elif branch:
        add("branch_matches_ifsc", "NOT_APPLICABLE", None, "branch not printed; taken from the IFSC master")

    # 8: holder
    if holder.status == X.PRESENT:
        low = holder.confidence < min_conf
        add("holder_name", "REVIEW" if low else "PASS", "HOLDER_NAME_LOW_CONFIDENCE" if low else None)
    else:
        add("holder_name", "REVIEW", {X.MISSING: "HOLDER_NAME_MISSING", X.AMBIGUOUS: "HOLDER_NAME_CONFLICT"}.get(
            holder.status, "HOLDER_NAME_UNREADABLE"))

    if any(c.result == "FAIL" for c in checks):
        return Verdict(REJECTED, reasons, checks)
    if any(c.result == "REVIEW" for c in checks):
        return Verdict(MANUAL_REVIEW, reasons, checks)
    return Verdict(VERIFIED, reasons, checks)


__all__ = ["Check", "ERROR", "MANUAL_REVIEW", "REJECTED", "UNREADABLE", "UNSUPPORTED", "VERIFIED", "Verdict",
           "classify", "decide"]
