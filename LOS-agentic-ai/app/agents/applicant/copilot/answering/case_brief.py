"""
THE CASE BRIEF on open (FINAL FIX A1 / A4; config product_flow.yaml `case_brief`).

    line 1   the case: id, name, stage
    line 2   the main status or blocker WITH its reason ("PAN and Aadhaar are not uploaded, so Aniket's KYC hasn't
             run yet") -- or "All FOS checks have passed" ONLY when no item of ANY group (counted or advisory) is
             pending, not run, failed or in review
    line 3   "Next step: ..."
    links    at most `max_suggestions`, chosen by the case's state

ONE SOURCE: the readiness report (readiness_report.build) -- the same items the readiness answer and the gate use,
so the brief can never contradict them.
"""

from __future__ import annotations

from typing import Any

PASS, PENDING, FAILED, REVIEW = "PASS", "PENDING", "FAILED", "REVIEW"


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.cfg().get("case_brief") or {}


def _say(key: str, lang: str, **values: Any) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.say((_cfg().get("texts") or {}).get(key), lang, **values)


def _join(items: list[str], lang: str) -> str:
    items = list(dict.fromkeys(i for i in items if i))
    word = _say("and", lang) or "and"
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {word} " + items[-1]


def _party_of(item_id: str) -> str:
    """'DOC:CO:PAN' / 'KYC:CO_APPLICANT:name' -> CO, else PRIMARY."""
    parts = str(item_id).split(":")
    return "CO" if len(parts) > 1 and parts[1].startswith("CO") else "PRIMARY"


def _names(case_id: str, application: Any) -> dict[str, str]:
    from app.agents.applicant.copilot.capabilities import workspace

    out = {"PRIMARY": workspace._name(application.applicant_id, short=False) if application else ""}
    try:
        from app.agents.los import co_applicants

        found = co_applicants.list_for_case(case_id) or []
        if found:
            out["CO"] = str(found[0].get("name") or "")
    except Exception:  # noqa: BLE001 - no co-applicant record: the applicant's name only
        pass
    return out


def build(case_id: str, lang: str) -> dict[str, Any]:
    """{text, suggestions, all_clear}: the three lines and the state-chosen questions."""
    from app.agents.applicant.copilot.answering import readiness_report
    from app.agents.applicant.copilot.capabilities import workspace
    from app.store import get_repository

    application = get_repository().get_application(case_id)
    stage = workspace._stage(case_id) or "--"
    names = _names(case_id, application)
    report = readiness_report.build(case_id)
    items = [i for g in report.get("groups") or [] for i in g.get("items") or []]
    open_items = [i for i in items if i.get("status") != PASS]
    order = {i: n for n, i in enumerate(report.get("unblock_order") or [])}
    open_items.sort(key=lambda i: order.get(i.get("id"), 999))
    head = _say("head", lang, case_id=case_id, name=names.get("PRIMARY") or "", stage=stage)
    status = _status_line(open_items, names, stage, report, lang)
    from app.agents.applicant.copilot.capabilities import case_form

    gaps = case_form.missing(case_id) if case_form.enabled() else []
    if gaps:
        # information first (the workflow's own order): a half-filled form is the main blocker
        status = case_form._say("missing_fields", lang, fields=", ".join(case_form.label(f, lang) for f in gaps))
    try:
        from app.agents.applicant.copilot.capabilities import timeline

        late = timeline.turnaround_line(case_id)
    except Exception:  # noqa: BLE001 - the flag is a convenience; the brief stands without it
        late = None
    if late:
        status = f"{status} {late}".strip()          # past the stage's target: said on the same status line
    all_clear = not open_items and not gaps and bool(report.get("ready"))
    if all_clear:
        from app.agents.applicant.copilot.capabilities import product_flow

        nexts = ((product_flow.cfg().get("stage_flow") or {}).get("chat_moves") or {}).get(stage.upper()) or []
        step = _say("next_ready", lang, next=nexts[0]) if nexts else ""
    else:
        step = str(report.get("next_fix") or "")
    lines = [head, status] + ([_say("next_step", lang, step=step.rstrip("."))  + "."] if step else [])
    key = "ready" if all_clear else "problems"
    from app.agents.applicant.copilot.answering import language_lock

    asked = language_lock.pick((_cfg().get("suggestions") or {}).get(key) or [], lang)
    return {"text": "\n".join(x for x in lines if x), "all_clear": all_clear,
            "suggestions": list(asked if isinstance(asked, list) else [asked])}


def _status_line(open_items: list[dict[str, Any]], names: dict[str, str], stage: str, report: dict[str, Any],
                 lang: str) -> str:
    """The ONE main status line, with its reason; consistent with every item by construction."""
    if not open_items:
        return _say("all_clear", lang, stage=stage) if report.get("ready") else ""
    bad = [i for i in open_items if i.get("status") in (FAILED, REVIEW)]
    if bad:
        first = bad[0]
        if first.get("status") == REVIEW:
            return _say("in_review", lang, what=first.get("label"))
        reason = str(first.get("reason") or "").rstrip(".") or str(first.get("fix") or "").rstrip(".")
        if str(first.get("id", "")).startswith("GATE:"):
            return _say("gate_blocked", lang, what=first.get("label"))
        return _say("failed", lang, what=first.get("label"), reason=reason)
    docs = [i for i in open_items if str(i.get("id", "")).startswith(("DOC:", "SIG:"))]
    kyc = [i for i in open_items if str(i.get("id", "")).startswith("KYC:")]
    kyc_docs = {str(d).upper() for d in _cfg().get("kyc_documents") or []}
    if kyc:
        party = _party_of(kyc[0]["id"])
        who = names.get(party) or names.get("PRIMARY") or ""
        blocking = [d["label"] for d in docs if _party_of(d["id"]) == party
                    and str(d["id"]).split(":")[-1].upper() in kyc_docs]
        if blocking:
            others = [d["label"] for d in docs if d["label"] not in blocking]
            line = _say("docs_and_kyc", lang, docs=_join(blocking + others, lang),
                        are=_say("are" if len(blocking + others) > 1 else "is", lang), who=who)
            return " ".join(line.split())
        if not docs:
            return _say("kyc_not_run", lang, who=who)
    if docs:
        return _say("docs_pending", lang, docs=_join([d["label"] for d in docs], lang))
    first = open_items[0]
    if str(first.get("id", "")).startswith("GATE:"):
        return _say("gate_blocked", lang, what=first.get("label"))
    return _say("docs_pending", lang, docs=first.get("label"))


def why_fallback(published: Any) -> Any:
    """
    "kyu?" / "why is it stuck?" on a case with NO recorded finding: never "nothing on file explaining it" when the
    case plainly waits on something -- the brief's status line and next step are the answer (both endpoints).
    """
    from app.agents.applicant import case_memory_facts

    if not isinstance(published, dict) or not published.get("case_id"):
        return published
    empty = case_memory_facts.NOTHING_RECORDED
    if empty not in str(published.get("answer") or ""):
        return published
    from app.agents.applicant.copilot.answering import language_lock

    brief = build(str(published["case_id"]), language_lock.current() or "en")
    if brief["all_clear"]:
        return published
    lines = brief["text"].split("\n")[1:]
    published["answer"] = str(published["answer"]).replace(empty, "\n".join(lines))
    published.pop("answer_markdown", None)
    return published


__all__ = ["build", "why_fallback"]
