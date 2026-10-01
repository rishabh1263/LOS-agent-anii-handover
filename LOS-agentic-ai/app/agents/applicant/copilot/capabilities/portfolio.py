"""
EVERY CASE OF AN APPLICANT, SUMMARISED: "APP-123 ke saare cases ka summary do",
"mere saare cases".

    applications.list (the applicant's cases, one read, already authorized
    for the applicant)
      -> EACH case authorized on its own (access.authorize_conversation):
         a case the caller may not open is dropped -- not counted, not named,
         its existence never disclosed
      -> ONE query for every authorized case's documents, ONE for their
         current findings (no N+1 over a portfolio)
      -> per case: stage / status, verification counts, KYC status,
         blockers, next step -- all RECORDED values
      -> an aggregate line, latest case first

NOTHING IS INFERRED. A status is the recorded status, a verdict the recorded
verdict, a KYC state the current KYC finding's; the next step for a case is
the same one the verification cards derive from its recorded verdicts.
"""

from __future__ import annotations

from typing import Any

import re

from app.agents.applicant.copilot.answering import structured

LATEST, PREVIOUS, ATTENTION = "LATEST", "PREVIOUS", "ATTENTION"


def focus_of(message: str) -> str | None:
    """Which case(s) the question is about: by position, or the ones in trouble."""
    text = str(message or "").lower()
    if re.search(r"\b(issues?|problems?|blocked|blocking|atk\w*|stuck|attention|dikkat|gadbad|fail\w*|reject\w*)\b", text) \
            and re.search(r"\b(kis|kaun\w*|konse|konsa|which)\s+(case|application)s?\b", text):
        return ATTENTION
    # ONE case by position -- "pichle case", "the latest application". A plural
    # or an "across / all" question ("what happened across my previous
    # cases?") is about every case, and gets the whole summary.
    if re.search(r"\b(across|all|every|saare|sare|sab|sabhi)\b", text):
        return None
    if re.search(r"\b(previous|past|pichl\w*|puran[aie]|older|earlier)\s+(case|application|loan)\b", text):
        return PREVIOUS
    if re.search(r"\b(latest|newest|most\s+recent)\s+(case|application|loan)\b", text):
        return LATEST
    return None


def focused(focus: str, block: dict[str, Any], lines: list[str]) -> str:
    """The answer for ONE position (or the cases in trouble), from the same rows."""
    cases = block["cases"]
    # NO ORDER TO GO BY: two cases opened at the same recorded instant (or no
    # time recorded) -- which is "latest" is not guessed
    if focus in (LATEST, PREVIOUS) and len(cases) >= 2 and (
            not cases[0].get("opened_at") or cases[0].get("opened_at") == cases[1].get("opened_at")):
        both = "".join(f"\n- {line.split(' -- ', 1)[-1]}" for line in lines[:2])
        return ("I can't tell which of these cases is the latest -- they were opened at the same "
                "recorded time. Here are both:" + both)
    if focus == LATEST:
        return f"Your latest case: {lines[0][len('Latest -- '):]}"
    if focus == PREVIOUS:
        if len(cases) < 2:
            return ("You have only one case, so there is no previous case. "
                    f"Your current case: {lines[0][len('Latest -- '):]}")
        rest = [line[len("Previous -- "):] for line in lines[1:]]
        if len(rest) == 1:
            return f"Your previous case: {rest[0]}"
        return "Your previous cases:" + "".join(f"\n- {line}" for line in rest)
    troubled = [line for line, case in zip(lines, cases) if case["blockers"]]
    if not troubled:
        return (f"None of your {len(cases)} case{'s' if len(cases) != 1 else ''} has anything "
                "blocking it right now.")
    return (f"{len(troubled)} of your {len(cases)} case{'s' if len(cases) != 1 else ''} "
            f"need{'s' if len(troubled) == 1 else ''} attention:"
            + "".join(f"\n- {line}" for line in troubled))


def _allowed(caller: Any, applicant_id: str | None, case_ids: list[str]) -> list[str]:
    from app.security import access

    kept = []
    for case_id in case_ids:
        try:
            # the CORE grant check (a case grant, or its applicant's), then the
            # conversation layer -- the same two every case read passes
            access.authorize(caller.subject, caller.scopes, applicant_id=applicant_id, case_id=case_id)
            access.authorize_conversation(caller.subject, caller.scopes,
                                          applicant_id=applicant_id, case_id=case_id)
            kept.append(case_id)
        except Exception:  # noqa: BLE001 - not the caller's: silently absent
            continue
    return kept


def _stamp(record: dict[str, Any]) -> str:
    return str(record.get("updated_at") or record.get("created_at") or "")


def summarise(results: dict[str, Any], *, caller: Any, applicant_id: str | None,
              focus: str | None = None) -> tuple[str, dict[str, Any]] | None:
    """(answer, structured portfolio) or None when there is no case to summarise."""
    from app.agents.applicant.copilot.answering.answer import _readable
    from app.store import get_repository

    applications = [a for a in ((results.get("applications.list") or {}).get("applications") or [])
                    if isinstance(a, dict) and a.get("case_id")]
    if not applications:
        return None
    allowed = set(_allowed(caller, applicant_id, [str(a["case_id"]) for a in applications]))
    # LATEST FIRST, by when each case was opened (one read for the applicant)
    opened: dict[str, str] = {}
    try:
        for record in get_repository().list_applications(str(applicant_id or "")):
            opened[str(record.case_id)] = str(getattr(record, "created_at", "") or "")
    except Exception:  # noqa: BLE001 - no timestamps: the listed order stands
        opened = {}
    applications = sorted((a for a in applications if str(a["case_id"]) in allowed),
                          key=lambda a: opened.get(str(a["case_id"])) or _stamp(a), reverse=True)
    if not applications:
        return None
    ids = [str(a["case_id"]) for a in applications]
    repository = get_repository()
    documents = repository.list_documents_for_cases(ids)
    findings = repository.get_current_findings_for_cases(ids)

    cases, lines = [], []
    for position, application in enumerate(applications):
        case_id = str(application["case_id"])
        docs = [{"document_id": d.document_id, "document_type": d.document_type,
                 "status": getattr(d.status, "value", d.status),
                 "verification_status": d.verification_status,
                 "reason_codes": list(d.reason_codes or []), "party_role": d.party_role}
                for d in documents.get(case_id, [])]
        verification = structured.verification_block(docs)
        kyc_rows = [f for f in findings.get(case_id, [])
                    if str(getattr(f.finding_kind, "value", f.finding_kind)) == "KYC"]
        kyc_status = str(kyc_rows[-1].status).upper() if kyc_rows else "NOT_RECORDED"
        blockers = [f"{e['label']} {'failed verification' if e['verdict'] == 'FAIL' else 'needs a reviewer'}"
                    for e in verification["documents"] if e["verdict"] in ("FAIL", "REVIEW")]
        if kyc_status in ("REVIEW", "FAIL"):
            blockers.append("KYC " + ("needs review" if kyc_status == "REVIEW" else "failed"))
        step = next((e["next_action"] for e in verification["documents"]
                     if e["verdict"] == "FAIL" and e.get("next_action")), None) or \
            next((e["next_action"] for e in verification["documents"]
                  if e["verdict"] == "REVIEW" and e.get("next_action")), None)
        status = str(application.get("status") or "UNKNOWN")
        product = application.get("product")
        entry = {
            "case_id": case_id, "position": "LATEST" if position == 0 else "PREVIOUS",
            "opened_at": opened.get(case_id) or None,
            "product": product, "status": status, "stage": application.get("stage"),
            "verification": verification["summary"], "needs_attention": verification["needs_attention"],
            "kyc_status": kyc_status, "blockers": blockers, "next_action": step,
        }
        cases.append(entry)
        counts = ", ".join(f"{n} {k.lower()}" for k, n in sorted(verification["summary"].items())) \
            or "no documents yet"
        said = (f"{'Latest' if position == 0 else 'Previous'} -- "
                f"{_readable(str(product)) + ' application' if product else 'application'}: "
                f"{_readable(status)}; documents: {counts}; KYC: "
                f"{'not recorded' if kyc_status == 'NOT_RECORDED' else _readable(kyc_status).lower()}")
        if blockers:
            said += f"; blocking: {', '.join(blockers)}"
        if step and step.get("label"):
            said += f"; next: {step['label'][0].lower()}{step['label'][1:]}"
        lines.append(said + ".")
    total = len(cases)
    blocked = sum(1 for c in cases if c["blockers"])
    head = (f"You have {total} case{'s' if total != 1 else ''}"
            + (f", {blocked} with something blocking it." if blocked else ", nothing blocking any of them."))
    block = {"count": total, "blocked": blocked, "cases": cases, "focus": focus}
    if focus:
        return focused(focus, block, lines), block
    return head + "\n" + "\n".join(f"- {line}" for line in lines), block


__all__ = ["ATTENTION", "LATEST", "PREVIOUS", "focus_of", "focused", "summarise"]
