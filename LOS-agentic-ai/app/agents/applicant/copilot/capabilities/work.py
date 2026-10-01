"""
PENDING WORK -- what is left on a case, who has to move each item, and what
the assistant can do about it itself:

  "jo pending hai kar do" / "do whatever is pending"      DO
  "abhi kya kar sakte ho?" / "what can you do for me now"  CAN
  "everything okay with my application?"                  HEALTH
  "what still needs me?"                                  NEEDS_ME

    authorized case (proven by the caller of this module)
      -> the CURRENT documents and checklist (tools, already read) and the
         current KYC findings (one query)
      -> each item classified: COMPLETED / PROCESSING / SYSTEM (the assistant
         can run it) / USER (a file or input is needed) / REVIEWER (a person
         decides) -- from RECORDED verdicts only
      -> DO: every SYSTEM item the action registry lets the assistant run
         (app/config/copilot_actions.yaml) is run through the EXISTING
         verification capability (parallel, durable jobs, worker processes),
         then the state is READ AGAIN -- a result is reported only as the
         store now records it
      -> the answer: what was done, what is still processing, what needs the
         person, what waits on a reviewer -- and ONE next step

NEVER A FALSE "DONE". An item is "completed" only when its recorded verdict
says so; a verification still running is PROCESSING; a failed read is
reported as not finished. Nothing is inferred and nothing is invented: an
item with no recorded state is not listed.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any

import yaml

from app.agents.applicant.copilot.answering import structured

DO, CAN, HEALTH, NEEDS_ME = "DO", "CAN", "HEALTH", "NEEDS_ME"
COMPLETED, PROCESSING, SYSTEM, USER, REVIEWER = "COMPLETED", "PROCESSING", "SYSTEM", "USER", "REVIEWER"

_DO = re.compile(
    r"\bjo\s+(kuch\s+|bhi\s+)?(pending\s+|baa?ki\s+)?(hai\s+)?(kar\s+(sakte|sakta|sakti)\s+ho\s+)?"
    r"(wo\s+|woh\s+|vo\s+)?kar\s*(d[oeiy]|dena|dijiye|lo)\b"
    r"|\bpending\s+(sab|sabhi|saare|sare|sara|kaam|items?|things?)\s+(kar|complete|finish|process|khatam)\w*"
    r"|\b(sab|sabhi|saara|sara|saare)\s+(pending\s+)?(complete|khatam|finish|process|poora|pura)\s*(kar\w*)?\b"
    r"|\b(do|handle|finish|complete|process|clear|take\s+care\s+of)\s+(whatever|everything|all(\s+the)?)\b"
    r"[^?]{0,25}\b(pending|remaining|left|outstanding|you\s+can|possible)\b"
    r"|\bhandle\s+(everything|it\s+all|all\s+of\s+it)\b"
    r"|\bje\s+(kahi\s+)?(pending|baki|baaki)\s+(aahe|ahe)\s+te\s+kar(a|un\s+tak\w*)?\b"
    r"|\bdo\s+(everything|all)\s+(you\s+can|that'?s\s+possible)\b",
    re.IGNORECASE)
_CAN = re.compile(
    r"\bwhat\s+can\s+you\s+do\s+(for\s+me\s+)?(now|right\s+now|here|on\s+(this|my)\s+(case|application)"
    r"|for\s+(this|my)\s+(case|application))\b"
    r"|\bwhat\s+can\s+you\s+do\s+for\s+me\b"
    r"|\b(abhi|ab)\s+(tum\s+|aap\s+)?kya\s+kar\s+(sakte|sakta|sakti)\b"
    r"|\bkya\s+kar\s+(sakte|sakta|sakti)\s+(ho\s+)?(abhi|ab|mere\s+liye)\b"
    r"|\bmere\s+liye\s+kya\s+kar\s+(sakte|sakta|sakti)\b",
    re.IGNORECASE)
_HEALTH = re.compile(
    r"\b(is\s+)?(everything|all|sab\s*kuch|sabkuch|sab)\s+(ok|okay|fine|good|theek|thik|sahi|set|alright)\b"
    r"|\bany\s+(issues?|problems?)\s+(with|in|on)\s+(my|this)\b"
    r"|\b(koi|kuch)\s+(issue|problem|dikkat|gadbad)\s+(hai|to\s+nahi|toh\s+nahi)\b",
    re.IGNORECASE)
_NEEDS_ME = re.compile(
    r"\bwhat\s+(still\s+)?needs\s+(me|my\s+(input|action))\b"
    r"|\bwhat\s+(do\s+)?you\s+(still\s+)?need\s+from\s+me\b"
    r"|\bwhat\s+is\s+(still\s+)?(needed|required)\s+from\s+me\b"
    r"|\b(mujhse|mere\s+se|mujhe\s+se)\s+kya\s+chahiye\b",
    re.IGNORECASE)
#: Questions about MANY cases belong to the portfolio, not to this one case.
_OTHER_CASES = re.compile(r"\b(kis|kaun\w*|which)\s+(case|application)s?\b|\b(cases|applications)\b", re.I)


_DOCUMENT_WORDS = re.compile(r"\b(paper\w*|document\w*|docs?|kagaz\w*|dastavez\w*)\b", re.I)


def about_documents(message: str) -> bool:
    """The question is about the papers specifically ("my paperwork", "mere papers")."""
    return bool(_DOCUMENT_WORDS.search(str(message or "")))


def _request_as_typed(message: str) -> str | None:
    """Which kind of pending-work request this is, or None."""
    text = " ".join(str(message or "").split())
    if not text or _OTHER_CASES.search(text):
        return None
    for mode, pattern in ((DO, _DO), (NEEDS_ME, _NEEDS_ME), (CAN, _CAN), (HEALTH, _HEALTH)):
        if pattern.search(text):
            return mode
    return None


@functools.lru_cache(maxsize=1)
def registry() -> dict[str, dict[str, Any]]:
    """The action registry (app/config/copilot_actions.yaml)."""
    path = Path(__file__).resolve().parents[4] / "config" / "copilot_actions.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {str(k): dict(v or {}) for k, v in (loaded.get("actions") or {}).items()}
    except Exception:  # noqa: BLE001 - no registry: the assistant runs nothing by itself
        return {}


def may_run(action: str, scopes: set[str]) -> bool:
    """The registry lets the assistant run `action` for this caller, unasked."""
    spec = registry().get(action) or {}
    if not spec.get("auto_execute") or spec.get("confirmation_required"):
        return False
    wanted = {str(s) for s in spec.get("required_scopes") or []}
    return not wanted or bool(wanted & scopes)


# ---- classification ---------------------------------------------------------------
_OWNER = {"PASS": COMPLETED, "PROCESSING": PROCESSING, "PENDING": SYSTEM, "FAIL": USER,
          "MISSING": USER, "REVIEW": REVIEWER}


def _whose(entry: dict[str, Any], multi_party: bool) -> str:
    if not multi_party:
        return ""
    return " (co-applicant)" if entry.get("party_role") == "CO_APPLICANT" else " (you)" \
        if entry.get("party_role") else ""


def _kyc_items(repository: Any, case_id: str) -> list[dict[str, Any]]:
    """The CURRENT KYC finding per party, as items (recorded status only)."""
    try:
        findings = repository.get_current_findings(case_id, kind="KYC")
    except Exception:  # noqa: BLE001 - no KYC read: no KYC item, never a guess
        return []
    latest: dict[str, Any] = {}
    for f in findings or []:
        latest[str(getattr(f, "party_id", "") or "")] = f
    items = []
    for f in latest.values():
        status = str(getattr(f, "status", "") or "").upper()
        if status not in ("PASS", "REVIEW", "FAIL"):
            continue
        codes = [str(c) for c in getattr(f, "reason_codes", []) or []]
        items.append({"item": "KYC", "label": "KYC", "document_id": None, "party_role": None,
                      "state": status, "owner": COMPLETED if status == "PASS" else REVIEWER,
                      "reason": structured._reason(codes), "reason_codes": codes,
                      "action": None if status == "PASS" else {"action": "AWAIT_REVIEW",
                                                                "label": "A reviewer needs to check the KYC"}})
    return items


def _queued(repository: Any, case_id: str) -> set[str]:
    try:
        from app.store.ocr_queue import OcrJobStatus

        return {str(j.document_id) for j in repository.get_ocr_jobs(case_id)
                if j.status is OcrJobStatus.QUEUED}
    except Exception:  # noqa: BLE001 - no queue: recorded verdicts stand
        return set()


def items_of(documents: list[dict[str, Any]], checklist: list[dict[str, Any]], *, repository: Any,
             case_id: str, can_run: bool, scores: dict[str, Any] | None = None,
             sources: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every item on the case with its state and who moves it forward."""
    block = structured.verification_block(documents, checklist, scores=scores, include_missing=True,
                                          sources=sources)
    # A DOCUMENT WHOSE DURABLE JOB IS STILL QUEUED has only a provisional verdict
    # (a long statement deferred on upload): its verification is still to run
    queued = _queued(repository, case_id)
    # a VERIFIED copy of the same type for the same person settles that type:
    # an earlier failed or reviewed attempt is history, not pending work
    settled = {(str(e.get("document_type")), str(e.get("party_role"))) for e in block["documents"]
               if e["verdict"] == "PASS"}
    items = []
    for e in block["documents"]:
        if "DOCUMENT_TYPE_MISMATCH" in (e.get("reason_codes") or []) and (
                str(e.get("document_type")), str(e.get("party_role"))) in settled:
            continue                          # a wrong-type attempt a verified copy replaced
        if e["verdict"] == "MISSING" and str(e.get("requirement") or "REQUIRED").upper() == "OPTIONAL":
            continue                                   # optional and absent: nothing is pending
        if e.get("document_id") and str(e["document_id"]) in queued and e["verdict"] != "PROCESSING":
            e = {**e, "verdict": "PENDING", "reason": None}
        owner = _OWNER.get(e["verdict"], REVIEWER)
        if owner == SYSTEM and str(e.get("document_type") or "UNKNOWN").upper() == "UNKNOWN":
            owner = USER                     # nothing to verify it AS: a readable upload is needed
        action = e.get("next_action")
        if owner == SYSTEM:
            action = {"action": "VERIFY_DOCUMENT", "document_id": e.get("document_id"),
                      "label": f"Verify the {e['label']}", "enabled": can_run}
        items.append({"item": e.get("document_type"), "label": e["label"], "document_id": e.get("document_id"),
                      "party_role": e.get("party_role"), "state": e["verdict"], "owner": owner,
                      "score": e.get("score"), "reason": e.get("reason"), "reason_codes": e.get("reason_codes"),
                      "source": e.get("source"), "action": action,
                      "executable": owner == SYSTEM and can_run})
    return items + _kyc_items(repository, case_id)


def _summary(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"user_action_required": 0, "system_action_required": 0, "processing": 0,
              "awaiting_review": 0, "completed": 0}
    key = {USER: "user_action_required", SYSTEM: "system_action_required", PROCESSING: "processing",
           REVIEWER: "awaiting_review", COMPLETED: "completed"}
    for i in items:
        counts[key[i["owner"]]] += 1
    return counts


# ---- the answer -------------------------------------------------------------------
def _said(item: dict[str, Any], multi_party: bool) -> str:
    name = f"{item['label']}{_whose(item, multi_party)}"
    if item["owner"] == USER:
        return (f"upload the {name}" if item["state"] == "MISSING"
                else f"upload a correct {name}" + (f" -- {item['reason'].rstrip('.').lower()}"
                                                    if item.get("reason") else ""))
    if item["owner"] == REVIEWER:
        return name + (f" -- {item['reason'].rstrip('.').lower()}" if item.get("reason") else "")
    return name


def _listed(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def compose(mode: str, items: list[dict[str, Any]], *, executed: dict[str, str],
            can_run: bool) -> tuple[str, dict[str, Any] | None]:
    """(answer, a yes/no offer or None) -- from the classified items only."""
    multi_party = len({i.get("party_role") for i in items if i.get("party_role")}) > 1
    by = {k: [i for i in items if i["owner"] == k] for k in (COMPLETED, PROCESSING, SYSTEM, USER, REVIEWER)}
    verified_now = [i for i in items if executed.get(str(i.get("document_id"))) == "VERIFIED_NOW"]
    lines: list[str] = []
    offer = None

    if mode == DO:
        if verified_now:
            lines.append("I verified " + _listed([
                f"the {i['label']}{_whose(i, multi_party)} ({ {'PASS': 'verified', 'FAIL': 'failed', 'REVIEW': 'needs a reviewer'}.get(i['state'], i['state'].lower()) })"
                for i in verified_now]) + ".")
        if by[PROCESSING]:
            lines.append("Still being verified: " + _listed([_said(i, multi_party) for i in by[PROCESSING]])
                         + ". I'll report the result once it's recorded.")
        if by[SYSTEM] and not can_run:
            lines.append("I can't start verification in this session (it needs upload permission): "
                         + _listed([_said(i, multi_party) for i in by[SYSTEM]]) + ".")
        if not executed and not by[PROCESSING] and not (by[SYSTEM] and not can_run):
            lines.append("There was nothing on this case I could run myself.")
    elif mode == CAN:
        runnable = [i for i in by[SYSTEM] if i.get("executable")]
        if runnable:
            lines.append("I can verify " + _listed([f"the {_said(i, multi_party)}" for i in runnable]) + " now.")
            offer = {"reason": "ACTION_OFFER", "question": "Shall I start the verification now?",
                     "options": ["verify all pending documents"]}
        else:
            lines.append("There's nothing I can run by myself on this case right now.")
    elif mode == HEALTH:
        attention = by[USER] + by[REVIEWER] + by[SYSTEM] + by[PROCESSING]
        if not attention:
            lines.append("Everything currently required is complete -- there are no pending document, "
                         "verification or KYC items on this case.")
        else:
            lines.append(f"{len(attention)} item{'s' if len(attention) != 1 else ''} need"
                         f"{'s' if len(attention) == 1 else ''} attention.")

    if by[USER]:
        lines.append(("You need to " if mode in (NEEDS_ME, DO, CAN) else "Needs you: ")
                     + _listed([_said(i, multi_party) for i in by[USER]]) + ".")
    elif mode == NEEDS_ME:
        lines.append("Nothing needs you right now.")
    if by[REVIEWER]:
        lines.append("Waiting on a reviewer: " + _listed([_said(i, multi_party) for i in by[REVIEWER]]) + ".")
    if mode in (HEALTH, NEEDS_ME) and by[SYSTEM]:
        runnable = [i for i in by[SYSTEM] if i.get("executable")]
        lines.append("Uploaded and not verified yet: " + _listed([_said(i, multi_party) for i in by[SYSTEM]])
                     + ("." if not runnable else " -- I can verify " + ("it" if len(runnable) == 1 else "them")
                        + " now."))
        if runnable and offer is None:
            offer = {"reason": "ACTION_OFFER", "question": "Shall I start the verification now?",
                     "options": ["verify all pending documents"]}
    if mode in (HEALTH, NEEDS_ME) and by[PROCESSING]:
        lines.append("Being verified now: " + _listed([_said(i, multi_party) for i in by[PROCESSING]]) + ".")

    # ONE NEXT STEP, from the state -- never a generic "anything else?"
    if offer:
        lines.append(offer["question"])
    elif by[USER]:
        first = by[USER][0]
        lines.append(f"Upload the {first['label']}{_whose(first, multi_party)} here and it's verified as part "
                     "of the upload.")
    elif mode == DO and not (by[USER] or by[REVIEWER] or by[SYSTEM] or by[PROCESSING]):
        lines.append("Everything currently required on this case is complete.")
    return " ".join(lines), offer


async def run(mode: str, *, documents: list[dict[str, Any]], checklist: list[dict[str, Any]],
              repository: Any, case_id: str, applicant_id: str, scopes: set[str]) -> dict[str, Any]:
    """The pending-work answer; for DO, after running what may be run."""
    from app.agents.applicant.copilot.capabilities import verification

    checklist = [r for r in checklist or [] if isinstance(r, dict)]
    can_run = may_run("VERIFY_DOCUMENT", scopes)
    items = items_of(documents, checklist, repository=repository, case_id=case_id, can_run=can_run)
    executed: dict[str, str] = {}
    processing = None
    if mode == DO and can_run and any(i["owner"] == SYSTEM for i in items):
        # THE EXISTING VERIFICATION CAPABILITY does the work (parallel, durable
        # jobs, worker processes); its refreshed documents are the new state
        out = await verification.run(verification.Request(verification.ALL), documents=documents,
                                     checklist=checklist, party=None, repository=repository,
                                     case_id=case_id, applicant_id=applicant_id, can_write=True)
        processing = out.get("processing") or {}
        refreshed = {str(e.get("document_id")): e for e in (out.get("verification") or {}).get("documents") or []}
        executed = {str(e.get("document_id")): str(e.get("source")) for e in refreshed.values()
                    if e.get("source") in ("VERIFIED_NOW", "PROCESSING", "FAILED")}
        # RE-READ, never assumed: the documents as the store records them now
        fresh = []
        for d in documents:
            record = repository.get_document(str(d.get("document_id"))) if d.get("document_id") else None
            if record is not None:
                status = getattr(record.status, "value", record.status)
                d = {**d, "status": status, "verification_status": record.verification_status,
                     "reason_codes": list(record.reason_codes or [])}
            if executed.get(str(d.get("document_id"))) == "PROCESSING":
                d = {**d, "status": "PROCESSING", "verification_status": None}
            fresh.append(d)
        documents = fresh
        items = items_of(documents, checklist, repository=repository, case_id=case_id, can_run=can_run,
                         sources=executed)
    answer, offer = compose(mode, items, executed=executed, can_run=can_run)
    actions = [i["action"] for i in items
               if i.get("action") and i["owner"] == USER]
    actions += [i["action"] for i in items if i["owner"] == SYSTEM and i.get("executable")]
    return {
        "answer": answer, "offer": offer,
        "response_type": "ACTION_RESULT" if mode == DO else "PENDING_WORK",
        "pending_work": {"mode": mode, "items": items, "summary": _summary(items),
                         "executed": [{"document_id": k, "result": v} for k, v in executed.items()],
                         "assistant_can_run": can_run},
        "processing": processing, "actions": actions, "documents": documents,
    }


__all__ = ["CAN", "DO", "HEALTH", "NEEDS_ME", "about_documents", "compose", "items_of", "may_run", "registry", "request", "run"]


def request(message: str) -> str | None:
    """As typed, else as its canonical English (a Hindi or Marathi request)."""
    found = _request_as_typed(message)
    if found is None and message:
        from app.agents.applicant import language

        canonical = language.canonicalise(message)
        if canonical.changed:
            found = _request_as_typed(canonical.text)
    return found
