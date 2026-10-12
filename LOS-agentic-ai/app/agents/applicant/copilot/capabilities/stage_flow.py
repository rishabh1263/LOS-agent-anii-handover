"""
THE FOS <-> CPA WORKFLOW IN CHAT (owner decision 2026-10-08; config product_flow.yaml `stage_flow`).

    FOS, something wrong   "Send query to customer": ONE query listing every problem (KYC mismatch, rejected and
                           missing documents) -> Confirm -> recorded as a CUSTOMER query (app.agents.los.queries) and
                           shown as the message to send. It blocks the move until the officer resolves it.
    FOS, all clear         "Move to CPA": the live gate (stage_gate.evaluate_live) must PASS -> Confirm -> the gated
                           transition, executed by the API route (the gate is re-checked at the move).
    CPA <-> FOS            "Raise query to FOS / CPA": the text -> Confirm -> a stage query along queries.yaml
                           routes; "reply to QRY-..." -> RESPONDED; "resolve QRY-..." -> RESOLVED.

Every write is a proposal + Confirm (the same `state.flow["write"]` as the case form). Scope is the ordinary
ownership check on every step; the move also needs one of `stage_flow.move_scopes`.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

KINDS = ("customer_query", "free_query", "move", "stage_query", "reply_query", "resolve_query",
         "deviation", "deviation_decision")
_QID = re.compile(r"\bQRY-[0-9A-F]{6,}\b", re.I)
_DEV_ID = re.compile(r"\bDEV-[0-9A-F]{10}\b", re.I)          # a raised deviation record
_DEV_CODE = re.compile(r"\bDVM-[0-9A-F]{6}\b", re.I)          # an entry of the deviation master


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.cfg().get("stage_flow") or {}


def enabled() -> bool:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.enabled() and bool(_cfg().get("enabled", False))


def _say(key: str, lang: str, **values: Any) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.say((_cfg().get("texts") or {}).get(key), lang, **values)


def _says(kind: str, text: str) -> bool:
    from app.agents.applicant.copilot.capabilities import product_flow

    return bool(product_flow._said(text, (_cfg().get("phrases") or {}).get(kind)))


def _stage(case_id: str) -> str:
    from app.agents.applicant.copilot.capabilities import workspace

    return str(workspace._stage(case_id) or "").upper()


def _label(stage: str) -> str:
    return stage or "--"


def _reply(request_id: str, case_id: str | None, answer: str, *, intent: str = "STAGE_FLOW", block: str = "",
           **extra: Any) -> dict[str, Any]:
    reply = {"request_id": request_id, "intent": intent, "answer": answer, "case_id": case_id,
             "category": "CASE_ONLY", "query_type": "CONVERSATION", "response_source": "STRUCTURED",
             "documents": [], "actions": [], "errors": [], "tools_invoked": [], "suggested_questions": [], **extra}
    if block:
        reply["faq_block"] = block                     # links travel apart: no rewrite step touches them
    return reply


def _confirm_links(ref: str, lang: str) -> str:
    from app.agents.applicant.copilot.answering import contract

    return contract.link("confirm_write", lang, ref=ref) + " · " + contract.link("cancel_write", lang, ref=ref)


def _write(kind: str, case_id: str, **values: Any) -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import case_form

    ttl = float(case_form._cfg().get("draft_ttl_seconds", 1800))
    return {"kind": kind, "case_id": case_id, "ref": f"{kind.split('_')[0]}-{uuid.uuid4().hex[:6]}",
            "expires": time.time() + ttl, **values}


# --------------------------------------------------------------------------
# what is wrong at FOS (from the case's own records) and the gate
# --------------------------------------------------------------------------

def problems(case_id: str, lang: str) -> list[str]:
    """One plain line per problem the customer must fix: KYC mismatches, rejected and missing documents."""
    from app.agents.applicant.copilot.answering import document_actions
    from app.agents.applicant.copilot.capabilities import product_flow

    view = document_actions.build(case_id)
    lines_cfg = (_cfg().get("texts") or {}).get("customer_line") or {}
    party_labels = _cfg().get("party_labels") or {}

    def party(row: dict[str, Any]) -> str:
        found = party_labels.get(str(row.get("party") or "PRIMARY_APPLICANT").upper())
        return product_flow.say(found, lang) if found else str(row.get("party_label") or "")

    out: list[str] = []
    for issue in view.get("kyc_issues") or []:
        values = ", ".join(f"{v.get('label')}: {v.get('value')}" for v in issue.get("values") or [] if v.get("value"))
        out.append(product_flow.say(lines_cfg.get("kyc"), lang, party=party(issue),
                                    field=issue.get("field_label") or issue.get("field") or "KYC", values=values))
    for row in view.get("reupload") or []:
        reason = (row.get("reasons") or [""])[0]
        out.append(product_flow.say(lines_cfg.get("reupload"), lang, party=party(row), document=row.get("label"),
                                    reason=reason))
    for row in view.get("pending") or []:
        out.append(product_flow.say(lines_cfg.get("pending"), lang, party=party(row), document=row.get("label")))
    return list(dict.fromkeys(x for x in out if x.strip()))


def gate(case_id: str) -> dict[str, Any]:
    from app.agents.los import stage_gate

    return stage_gate.evaluate_live(case_id, _stage(case_id))


def offers(case_id: str | None, lang: str) -> list[str]:
    """The next workflow step for this case, as ask: links: query the customer / move to CPA / query the other stage."""
    from app.agents.applicant.copilot.answering import contract
    from app.agents.applicant.copilot.capabilities import product_flow

    if not case_id or not enabled():
        return []
    o = _cfg().get("offers") or {}
    stage = _stage(case_id)
    try:
        if stage == "FOS":
            if problems(case_id, lang):
                return [contract.ask(product_flow.say(o.get("customer_query"), lang))]
            if gate(case_id).get("status") == "PASS":
                return [contract.ask(product_flow.say(o.get("move"), lang))]
            return [contract.ask(product_flow.say(o.get("to_cpa"), lang))]
        if stage == "CPA":
            return [contract.ask(product_flow.say(o.get("to_fos"), lang))]
    except Exception:  # noqa: BLE001 - an offer is a convenience; the answer stands without it
        return []
    return []


# --------------------------------------------------------------------------
# proposals (nothing is written here)
# --------------------------------------------------------------------------

def propose(message: str, case_id: str, claims: dict[str, Any], request_id: str,
            lang: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(reply, pending write) when the message asks for a workflow step on the open case, else (None, None)."""
    from app.agents.applicant.copilot.capabilities import workspace

    if not enabled():
        return None, None
    text = str(message or "")
    # DEVIATIONS FIRST ("raise deviation ..." must never read as "raise query"): the deviation master's entries only
    if _says("deviation_status", text):
        workspace.authorize(claims, case_id)
        return _deviation_list(case_id, request_id, lang), None
    if _says("approve_deviation", text) or _says("reject_deviation", text):
        workspace.authorize(claims, case_id)
        return _propose_deviation_decision(case_id, text, request_id, lang)
    if _says("raise_deviation", text):
        workspace.authorize(claims, case_id)
        return _propose_deviation(case_id, text, request_id, lang)
    # the most specific first: "raise query to FOS" is a stage query, not the customer one
    if _says("query_to_stage", text):
        workspace.authorize(claims, case_id)
        target = "FOS" if re.search(r"\bfos\b", text, re.I) else "CPA"
        body = re.split(r"(?i)\b(?:fos|cpa)\b\s*(?:ko\s+)?(?:query\s*)?(?:bhejo|bolo)?\s*[:,-]?", text, maxsplit=1)
        rest = body[1].strip(" :-,") if len(body) > 1 else ""
        return _propose_stage_query(case_id, target, rest, request_id, lang)
    if _says("query_status", text):
        workspace.authorize(claims, case_id)
        return _query_status(case_id, request_id, lang), None
    if _says("customer_query", text) or _says("free_query", text):
        workspace.authorize(claims, case_id)
        typed = _query_text(text)
        if typed:
            # "raise a query: address mismatch with Aadhaar" -- THE OFFICER'S words, drafted, then Confirm
            return _propose_free_query(case_id, typed, request_id, lang)
        if _says("free_query", text):
            # an explicit "raise a query" with no words yet: ask for them (at FOS the problem list is "collect")
            write = _write("free_query", case_id, text=None)
            return _reply(request_id, case_id, _say("ask_query_text", lang)), write
        return _propose_customer_query(case_id, request_id, lang)
    if _says("move", text):
        workspace.authorize(claims, case_id)
        return _propose_move(case_id, claims, request_id, lang)
    named = _QID.search(text)
    if _says("resolve_query", text):
        workspace.authorize(claims, case_id)
        return _propose_query_move(case_id, named.group(0).upper() if named else None, "RESOLVED", "", request_id,
                                   lang)
    if _says("reply_query", text) and named:
        workspace.authorize(claims, case_id)
        note = text[named.end():].strip(" :-,")
        return _propose_query_move(case_id, named.group(0).upper(), "RESPONDED", note, request_id, lang)
    return None, None


def _propose_customer_query(case_id: str, request_id: str, lang: str):
    found = problems(case_id, lang)
    if not found:
        return _reply(request_id, case_id, _say("nothing_wrong", lang, case_id=case_id)), None
    if _stage(case_id) == "FOS" and str(_cfg().get("fos_customer_mode") or "collect") == "collect":
        # AT FOS, NO QUERY (owner 2026-10-08): the officer is told what to COLLECT from the customer -- everything
        # pending, with an Upload link per document. Nothing is recorded, nothing to confirm.
        from app.agents.applicant.copilot.answering import document_actions

        view = document_actions.build(case_id)
        listed = "\n".join(f"{n}. {line}" for n, line in enumerate(found, 1))
        reply = _reply(request_id, case_id, _say("collect_heading", lang, case_id=case_id) + "\n\n" + listed,
                       intent="COLLECT_FROM_CUSTOMER")
        reply["document_actions"] = {"reupload": view.get("reupload") or [], "pending": view.get("pending") or []}
        return reply, None
    message = "\n".join(f"{n}. {line}" for n, line in enumerate(found, 1))
    write = _write("customer_query", case_id, text=" ".join(found)[:950], message=message)
    answer = _say("customer_draft", lang, case_id=case_id) + "\n\n" + "\n".join(f"> {ln}" for ln in message.split("\n"))
    return _reply(request_id, case_id, answer, block=_confirm_links(write["ref"], lang),
                  intent="CUSTOMER_QUERY_DRAFT"), write


def _move_allowed(claims: dict[str, Any]) -> bool:
    from app.security import access

    held = set(access.get_scopes(claims))
    return bool(held & {str(s) for s in _cfg().get("move_scopes") or []})


def _propose_move(case_id: str, claims: dict[str, Any], request_id: str, lang: str):
    here = _stage(case_id)
    targets = [str(t).upper() for t in (_cfg().get("chat_moves") or {}).get(here) or []]
    if not targets:
        return _reply(request_id, case_id, _say("not_this_stage", lang, case_id=case_id, here=here)), None
    there = targets[0]
    if not _move_allowed(claims):
        return _reply(request_id, case_id, _say("no_move_rights", lang)), None
    evaluated = gate(case_id)
    if evaluated.get("status") != "PASS":
        blockers = [f"- {c.get('label')}" for c in evaluated.get("blockers") or [] if c.get("label")]
        from app.agents.applicant.copilot.answering import readiness_report

        if readiness_report.enabled():
            # THE SAME ITEMS AS "is my case ready" (owner 2026-10-09: two phrasings gave two answers, this one only
            # "FOS requirements / KYC verification"): every failing item with its reason and fix
            try:
                report = readiness_report.build(case_id)
                if report.get("groups") and not report.get("ready"):
                    blockers = [readiness_report.render(report).split("\n", 1)[-1].strip()]
            except Exception:  # noqa: BLE001 - the gate's own labels stand
                pass
        listed = problems(case_id, lang)
        answer = "\n".join([_say("not_ready", lang, case_id=case_id, there=there), *blockers])
        from app.agents.applicant.copilot.answering import contract
        from app.agents.applicant.copilot.capabilities import product_flow

        block = contract.ask(product_flow.say((_cfg().get("offers") or {}).get("customer_query"), lang)) \
            if listed else ""
        return _reply(request_id, case_id, answer, block=block, intent="MOVE_NOT_READY"), None
    write = _write("move", case_id, here=here, there=there)
    return _reply(request_id, case_id, _say("move_proposal", lang, case_id=case_id, here=here, there=there),
                  block=_confirm_links(write["ref"], lang), intent="MOVE_PROPOSAL"), write


def _query_text(message: str) -> str | None:
    """The query's own words after the ask ("raise a query: <text>", "customer se query bhejo ki <text>"), or None."""
    text = " ".join(str(message or "").split())
    spec = _cfg().get("free_query") or {}
    for phrase in sorted([*(_cfg().get("phrases") or {}).get("free_query", []),
                          *(_cfg().get("phrases") or {}).get("customer_query", [])], key=len, reverse=True):
        found = re.search(rf"(?i)\b{re.escape(phrase)}\b", text)
        if found:
            rest = text[found.end():]
            rest = re.sub(r"(?i)^\s*(?:[:,\-–]|\bki\b|\bthat\b|\babout\b|\bfor\b|\bregarding\b)\s*", "", rest).strip()
            return rest if len(rest.split()) >= int(spec.get("min_words", 2)) else None
    return None


def _query_type(text: str) -> str:
    """The configured query type the words point at (stage_flow.free_query.types), else the default."""
    spec = _cfg().get("free_query") or {}
    said = text.lower()
    for kind, words in (spec.get("types") or {}).items():
        if any(re.search(rf"\b{re.escape(w)}\b", said) for w in words):
            return kind
    return str(spec.get("default_type") or "CLARIFICATION")


def _propose_free_query(case_id: str, text: str, request_id: str, lang: str):
    target = str((_cfg().get("free_query") or {}).get("target_type") or "CUSTOMER")
    write = _write("free_query", case_id, text=text[:950], target_type=target, query_type=_query_type(text))
    answer = _say("free_draft", lang, case_id=case_id, target=_say(f"target_{target.lower()}", lang),
                  query_type=_readable_type(write["query_type"])) + "\n\n> " + write["text"]
    return _reply(request_id, case_id, answer, block=_confirm_links(write["ref"], lang),
                  intent="QUERY_DRAFT"), write


def _readable_type(kind: str) -> str:
    return str(kind).replace("_", " ").capitalize()


def _query_status(case_id: str, request_id: str, lang: str) -> dict[str, Any]:
    """Every query on the case: id, status, to whom, what (read from app.agents.los.queries, the route's own store)."""
    from app.agents.los import queries

    found = queries.list_queries(case_id)
    if not found:
        return _reply(request_id, case_id, _say("no_queries", lang, case_id=case_id), intent="QUERY_STATUS")
    head = _say("queries_heading", lang, case_id=case_id, n=len(found))
    cols = _say("queries_columns", lang).split("|")
    rows = [f"| {q.get('query_id')} | **{q.get('status')}** | {_target_of(q, lang)} | "
            f"{_readable_type(q.get('query_type') or '')} | {str(q.get('text') or '')[:80].replace('|', '/')} |"
            for q in found]
    table = "\n".join(["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols), *rows])
    return _reply(request_id, case_id, f"{head}\n\n{table}", intent="QUERY_STATUS", tools_invoked=["los.query.list"])


def _target_of(query: dict[str, Any], lang: str) -> str:
    target = str(query.get("target_type") or "")
    if target == "STAGE" and query.get("target_stage"):
        return str(query.get("target_stage"))
    return _say(f"target_{target.lower()}", lang) if target else ""


def _propose_stage_query(case_id: str, target: str, text: str, request_id: str, lang: str):
    from app.agents.los import queries

    here = _stage(case_id)
    routes = [str(s).upper() for s in ((queries.config("queries").get("routes") or {}).get(here) or [])]
    if target not in routes:
        return _reply(request_id, case_id, _say("not_this_stage", lang, case_id=case_id, here=here)), None
    write = _write("stage_query", case_id, here=here, there=target, text=text or None)
    if not text:
        return _reply(request_id, case_id, _say("ask_text", lang, there=target)), write
    answer = _say("stage_query_proposal", lang, case_id=case_id, here=here, there=target) + "\n\n> " + text
    return _reply(request_id, case_id, answer, block=_confirm_links(write["ref"], lang)), write


def _open_queries(case_id: str) -> list[dict[str, Any]]:
    from app.agents.los import queries

    open_statuses = set(queries.config("queries").get("open_statuses") or [])
    return [q for q in queries.list_queries(case_id) if q.get("status") in open_statuses]


def _propose_query_move(case_id: str, query_id: str | None, to_status: str, note: str, request_id: str, lang: str):
    open_ones = _open_queries(case_id)
    if not open_ones:
        return _reply(request_id, case_id, _say("no_open_query", lang, case_id=case_id)), None
    if query_id is None and len(open_ones) == 1:
        query_id = str(open_ones[0].get("query_id"))
    if query_id is None or query_id not in {str(q.get("query_id")) for q in open_ones}:
        ids = ", ".join(str(q.get("query_id")) for q in open_ones)
        return _reply(request_id, case_id, _say("which_query", lang, ids=ids)), None
    key = "resolve_proposal" if to_status == "RESOLVED" else "reply_proposal"
    write = _write("resolve_query" if to_status == "RESOLVED" else "reply_query", case_id, query_id=query_id,
                   to_status=to_status, note=note or None)
    answer = _say(key, lang, query_id=query_id) + (("\n\n> " + note) if note else "")
    return _reply(request_id, case_id, answer, block=_confirm_links(write["ref"], lang)), write


# --------------------------------------------------------------------------
# the pending step: text (for a stage query), Confirm, Cancel
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# DEVIATIONS (the deviation master, queries.yaml deviations; 2026-10-12)
# --------------------------------------------------------------------------

def _dev_fields(entry: dict[str, Any]) -> dict[str, str]:
    return {"description": str(entry.get("description") or ""),
            "category": str(entry.get("category") or "").replace("_", " ").title().replace("Nfa", "NFA"),
            "approvers": ", ".join(str(a).replace("_", " ") for a in entry.get("approvers") or []) or "--"}


def _propose_deviation(case_id: str, text: str, request_id: str, lang: str):
    """ "raise deviation LTV > 70%": the master entry it names -> its reason -> Confirm. Not clear: pick from options."""
    from app.agents.los import queries

    code = _DEV_CODE.search(text)
    entry = queries.deviation_by_code(code.group(0).upper()) if code else None
    rest = text
    for phrase in sorted((_cfg().get("phrases") or {}).get("raise_deviation") or [], key=len, reverse=True):
        rest = re.sub(re.escape(phrase), " ", rest, flags=re.I)
    reason = None
    if ":" in rest:                                    # "raise deviation LTV > 70%: property value revised"
        rest, reason = rest.split(":", 1)
    words = " ".join(_DEV_CODE.sub(" ", rest).split()).strip(" -,")
    if entry is None and words:
        cfg = queries.config("deviations")
        ranked = queries.match_deviations(words, limit=3)
        top = ranked[0] if ranked else None
        margin = top[1] - ranked[1][1] if len(ranked) > 1 else 1.0
        if top and top[1] >= float(cfg.get("match_min_score", 0.5)) and margin >= float(cfg.get("match_margin", 0.15)):
            entry = top[0]
        elif top and top[1] > 0.15:
            # NOT CLEAR: the officer picks -- a write is never made on a guess
            # only the readings NEAR the best one (queries.yaml deviations.pick_min_share): "low cibil score" offered
            # "BT for loan < 10 MOB" at a third of the best score (live smoke 2026-10-12)
            share = float(cfg.get("pick_min_share", 0.6))
            lines = [f"- [{d['description'][:90]}](ask:{_ask_text(f'raise deviation ' + d['code'])})"
                     for d, s in ranked if s >= top[1] * share]
            return _reply(request_id, case_id, _say("deviation_pick", lang) + "\n" + "\n".join(lines),
                          intent="DEVIATION_PICK"), None
    if entry is None:
        return _reply(request_id, case_id, _say("deviation_none", lang, text=words or text)), None
    write = _write("deviation", case_id, code=entry["code"], reason=" ".join(str(reason or "").split()) or None)
    if not write["reason"]:
        return _reply(request_id, case_id, _say("deviation_ask_reason", lang, case_id=case_id, **_dev_fields(entry)),
                      intent="DEVIATION_DRAFT"), write
    return _deviation_draft(write, entry, request_id, lang), write


def _ask_text(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def _deviation_draft(write: dict[str, Any], entry: dict[str, Any], request_id: str, lang: str) -> dict[str, Any]:
    return _reply(request_id, write["case_id"], _say("deviation_draft", lang, case_id=write["case_id"],
                                                     reason=write["reason"], **_dev_fields(entry)),
                  block=_confirm_links(write["ref"], lang), intent="DEVIATION_DRAFT")


def _deviation_list(case_id: str, request_id: str, lang: str) -> dict[str, Any]:
    from app.agents.los import queries

    rows = queries.list_deviations(case_id)
    if not rows:
        return _reply(request_id, case_id, _say("deviation_list_none", lang, case_id=case_id), intent="DEVIATIONS")
    lines = [_say("deviation_list_head", lang, case_id=case_id)]
    for d in rows:
        lines.append(_say("deviation_row", lang, deviation_id=d.get("deviation_id"), status=d.get("status"),
                          **_dev_fields(d)))
    return _reply(request_id, case_id, "\n".join(lines), intent="DEVIATIONS", tools_invoked=["los.deviation.list"])


def _propose_deviation_decision(case_id: str, text: str, request_id: str, lang: str):
    """ "approve deviation DEV-...: reason" / "reject deviation DEV-...: reason" -> Confirm (authority checked then)."""
    from app.agents.los import queries

    decision = "REJECTED" if _says("reject_deviation", text) else "APPROVED"
    verb = "reject" if decision == "REJECTED" else "approve"
    named = _DEV_ID.search(text)
    open_rows = [d for d in queries.list_deviations(case_id)
                 if d["status"] in set(queries.config("deviations").get("open_statuses") or [])]
    if named is None:
        if len(open_rows) == 1:
            deviation_id = str(open_rows[0]["deviation_id"])
        else:
            ids = ", ".join(str(d["deviation_id"]) for d in open_rows) or "--"
            return _reply(request_id, case_id, _say("deviation_which", lang, ids=ids)), None
    else:
        deviation_id = named.group(0).upper()
    row = next((d for d in queries.list_deviations(case_id) if d["deviation_id"] == deviation_id), None)
    if row is None:
        return _reply(request_id, case_id, _say("deviation_which", lang,
                                                ids=", ".join(str(d["deviation_id"]) for d in open_rows) or "--")), None
    justification = text[named.end():].strip(" :-,") if named else (text.split(":", 1)[1].strip() if ":" in text else "")
    if not justification:
        return _reply(request_id, case_id, _say("deviation_need_reason", lang, verb=verb, deviation_id=deviation_id)), None
    write = _write("deviation_decision", case_id, deviation_id=deviation_id, decision=decision,
                   justification=justification[:500])
    return _reply(request_id, case_id, _say("deviation_decision_draft", lang, decision=decision.title(),
                                            deviation_id=deviation_id, justification=write["justification"],
                                            **_dev_fields(row)),
                  block=_confirm_links(write["ref"], lang), intent="DEVIATION_DECISION_DRAFT"), write


def step(write: dict[str, Any], message: str, claims: dict[str, Any], request_id: str,
         lang: str) -> dict[str, Any] | None:
    """The answer to a pending workflow proposal; None when the message is about something else (proposal dropped)."""
    from app.agents.applicant.copilot.capabilities import case_form, workspace

    ref = str(write.get("ref") or "")
    case_id = str(write.get("case_id") or "")
    if write.get("kind") == "deviation" and not write.get("reason"):
        # the deviation's reason was asked: THIS message is it -> the draft, then Confirm
        from app.agents.los import queries

        text = " ".join(str(message or "").split())
        if not text or case_form._only(message, [*case_form._words("cancel_words"), *case_form._words("soft_cancel_words"), "cancel"], ref):
            write["done"] = True
            return _reply(request_id, case_id, _say("cancelled", lang))
        write["reason"] = text[:500]
        return _deviation_draft(write, queries.deviation_by_code(write["code"]) or {}, request_id, lang)
    if write.get("kind") == "free_query" and not write.get("text"):
        # "raise a query" with no words yet: this message IS the query's text -> the draft, then Confirm
        text = " ".join(str(message or "").split())
        if not text or case_form._only(message, [*case_form._words("cancel_words"), *case_form._words("soft_cancel_words"), "cancel"], ref):
            write["done"] = True
            return _reply(request_id, case_id, _say("cancelled", lang))
        target = str((_cfg().get("free_query") or {}).get("target_type") or "CUSTOMER")
        write.update(text=text[:950], target_type=target, query_type=_query_type(text))
        answer = _say("free_draft", lang, case_id=case_id, target=_say(f"target_{target.lower()}", lang),
                      query_type=_readable_type(write["query_type"])) + "\n\n> " + write["text"]
        return _reply(request_id, case_id, answer, block=_confirm_links(ref, lang), intent="QUERY_DRAFT")
    if write.get("kind") == "stage_query" and not write.get("text"):
        text = " ".join(str(message or "").split())
        if not text or case_form._only(message, [*case_form._words("cancel_words"), *case_form._words("soft_cancel_words"), "cancel"], ref):
            write["done"] = True
            return _reply(request_id, case_id, _say("cancelled", lang))
        write["text"] = text[:950]
        answer = _say("stage_query_proposal", lang, case_id=case_id, here=write["here"], there=write["there"]) \
            + "\n\n> " + write["text"]
        return _reply(request_id, case_id, answer, block=_confirm_links(ref, lang))
    if case_form._only(message, [*case_form._words("cancel_words"), *case_form._words("soft_cancel_words"), "cancel"], ref):
        write["done"] = True
        return _reply(request_id, case_id, _say("cancelled", lang))
    if not case_form._only(message, [*case_form._words("confirm_words"), "confirm"], ref):
        return None
    workspace.authorize(claims, case_id)                       # scope re-checked at the write
    write["done"] = True
    return _execute(write, claims, request_id, lang)


def _execute(write: dict[str, Any], claims: dict[str, Any], request_id: str, lang: str) -> dict[str, Any]:
    from app.agents.applicant import audit
    from app.agents.applicant.copilot.capabilities import case_form
    from app.agents.los import queries
    from app.security import access
    from app.security.auth import get_subject

    case_id, kind = str(write["case_id"]), str(write["kind"])
    actor = str(get_subject(claims) or "")
    scopes = set(access.get_scopes(claims))
    try:
        if kind == "customer_query":
            created = queries.raise_query(case_id, target_type="CUSTOMER", query_type="MISSING_INFORMATION",
                                          text=str(write["text"]), actor=actor, scopes=scopes, request_id=request_id,
                                          idempotency_key=f"chat:{write['ref']}")
            case_form.activity(case_id, "CUSTOMER_QUERY", f"query {created.get('query_id')} to the customer by "
                                                          f"{actor} (chat)")
            answer = _say("customer_sent", lang, query_id=created.get("query_id")) + "\n\n" + \
                "\n".join(f"> {ln}" for ln in str(write.get("message") or "").split("\n"))
            from app.agents.applicant.copilot.answering import contract

            return _reply(request_id, case_id, answer, block=contract.link("copy", lang, ref="draft-1"),
                          intent="CUSTOMER_QUERY_SENT", tools_invoked=["los.query.raise"])
        if kind == "free_query":
            # THE SAME CALL as POST /api/v1/los/cases/{case_id}/queries (queries.raise_query: scopes, routes,
            # idempotency), made only after Confirm
            # `subject`: the officer's own words -- two different questions are two queries; the SAME question still
            # open returns the existing one (queries.raise_query's rule, as on the POST route) and is SAID to exist
            created = queries.raise_query(case_id, target_type=str(write.get("target_type") or "CUSTOMER"),
                                          query_type=str(write.get("query_type") or "CLARIFICATION"),
                                          text=str(write["text"]), actor=actor, scopes=scopes, request_id=request_id,
                                          idempotency_key=f"chat:{write['ref']}",
                                          subject=" ".join(str(write["text"]).split())[:200])
            if created.get("result") == "EXISTING":
                return _reply(request_id, case_id, _say("free_exists", lang, query_id=created.get("query_id"),
                                                        status=created.get("status") or "OPEN"),
                              intent="QUERY_EXISTS", query_id=created.get("query_id"))
            case_form.activity(case_id, "QUERY_RAISED", f"query {created.get('query_id')} raised by {actor} (chat)")
            return _reply(request_id, case_id, _say("free_sent", lang, query_id=created.get("query_id"),
                                                    status=created.get("status") or "OPEN")
                          + "\n\n> " + str(write["text"]), intent="QUERY_RAISED", tools_invoked=["los.query.raise"],
                          query_id=created.get("query_id"))
        if kind == "stage_query":
            created = queries.raise_query(case_id, target_type="STAGE", query_type="CLARIFICATION",
                                          text=str(write["text"]), actor=actor, scopes=scopes,
                                          target_stage=str(write["there"]), request_id=request_id,
                                          idempotency_key=f"chat:{write['ref']}")
            return _reply(request_id, case_id, _say("stage_query_sent", lang, query_id=created.get("query_id"),
                                                    here=write["here"], there=write["there"]),
                          intent="STAGE_QUERY_SENT", tools_invoked=["los.query.raise"])
        if kind in ("reply_query", "resolve_query"):
            queries.move_query(case_id, str(write["query_id"]), to_status=str(write["to_status"]), actor=actor,
                               scopes=scopes, note=write.get("note"), request_id=request_id)
            key = "resolved" if kind == "resolve_query" else "replied"
            return _reply(request_id, case_id, _say(key, lang, query_id=write["query_id"]),
                          intent="QUERY_UPDATED", tools_invoked=["los.query.move"])
        if kind == "move":
            return _move(write, claims, actor, request_id, lang)
        if kind == "deviation":
            # THE SAME SERVICE the decision route reads (queries.raise_deviation: scopes, master, idempotency)
            created = queries.raise_deviation(case_id, code=str(write["code"]), reason=str(write["reason"]),
                                              actor=actor, scopes=scopes, idempotency_key=f"chat:{write['ref']}",
                                              request_id=request_id)
            key = "deviation_exists" if created.get("result") == "EXISTING" else "deviation_raised"
            if key == "deviation_raised":
                case_form.activity(case_id, "DEVIATION_RAISED",
                                   f"deviation {created.get('deviation_id')} ({created.get('code')}) raised by {actor} (chat)")
            return _reply(request_id, case_id, _say(key, lang, deviation_id=created.get("deviation_id"),
                                                    status=created.get("status"), **_dev_fields(created)),
                          intent="DEVIATION_RAISED" if key == "deviation_raised" else "DEVIATION_EXISTS",
                          tools_invoked=["los.deviation.raise"], deviation_id=created.get("deviation_id"))
        if kind == "deviation_decision":
            return _decide_deviation(write, claims, actor, scopes, request_id, lang)
    except queries.QueryError as exc:
        if kind.startswith("deviation"):
            audit.record(request_id=request_id, subject=actor, applicant_id=None, case_id=case_id,
                         intent=kind.upper(), tools=[], write=True, status="REFUSED", detail=exc.code)
            return _reply(request_id, case_id, _say("deviation_failed", lang, why=exc.message))
        audit.record(request_id=request_id, subject=actor, applicant_id=None, case_id=case_id, intent=kind.upper(),
                     tools=[], write=True, status="REFUSED", detail=exc.code)
        return _reply(request_id, case_id, _say("query_failed", lang, why=exc.message))
    return _reply(request_id, case_id, _say("cancelled", lang))


def _decide_deviation(write: dict[str, Any], claims: dict[str, Any], actor: str, scopes: set[str], request_id: str,
                      lang: str) -> dict[str, Any]:
    """
    The same rules as POST /api/v1/los/cases/{case_id}/deviations/{id}/decision: the configured approving scope, the
    master's authority for THIS deviation, never the raiser, a justification -- and an APPROVAL under four eyes is
    only REQUESTED here (a second person checks it); a rejection acts at once.
    """
    from app.agents.los import queries
    from app.approvals import service as approvals
    from app.security.auth import get_roles

    case_id, deviation_id, decision = str(write["case_id"]), str(write["deviation_id"]), str(write["decision"])
    authorities = queries.authorities_of(get_roles(claims))
    if decision == "APPROVED" and approvals.requires_check("DEVIATION_APPROVAL"):
        cfg = queries.config("deviations")
        if not set(scopes) & set(cfg.get("approve_scopes") or []):
            raise queries.QueryError("DEVIATION_APPROVAL_NOT_PERMITTED",
                                     "Only the configured approving authority can approve a deviation.", 403)
        row = queries._get(case_id, "DEVIATION", deviation_id)
        if str((row.payload or {}).get("raised_by") or "") == actor:
            raise queries.QueryError("RAISER_CANNOT_DECIDE", "The person who raised a deviation cannot approve it.", 403)
        queries.check_deviation_authority(row, authorities)
        try:
            pending = approvals.request("DEVIATION_APPROVAL", case_id=case_id, resource_id=deviation_id,
                                        maker_id=actor, reason=str(write["justification"]),
                                        payload={"decision": "APPROVED", "justification": write["justification"],
                                                 "maker_scopes": sorted(scopes),
                                                 "maker_authorities": sorted(authorities)})
        except approvals.ApprovalError as exc:
            raise queries.QueryError(exc.code, exc.message, exc.http_status) from None
        return _reply(request_id, case_id, _say("deviation_pending_check", lang, deviation_id=deviation_id,
                                                approval_id=approvals.public(pending).get("approval_id")),
                      intent="DEVIATION_APPROVAL_PENDING_CHECK", tools_invoked=["approvals.request"])
    decided = queries.decide_deviation(case_id, deviation_id, decision=decision, actor=actor, scopes=scopes,
                                       justification=str(write["justification"]), request_id=request_id,
                                       authorities=authorities)
    return _reply(request_id, case_id, _say("deviation_decided", lang, deviation_id=deviation_id,
                                            status=decided.get("status")),
                  intent="DEVIATION_DECIDED", tools_invoked=["los.deviation.decide"])


def _move(write: dict[str, Any], claims: dict[str, Any], actor: str, request_id: str, lang: str) -> dict[str, Any]:
    """
    The confirmed move is NOT made here: no copilot module reaches the stage-transition service (an architecture
    rule, guarded by the stage-lifecycle tests, test_l). The reply carries `execute_move`; the API route executes it through the
    gated transition under the user's own identity (fos_api.move_case_stage).
    """
    case_id, here, there = str(write["case_id"]), str(write["here"]), str(write["there"])
    if not _move_allowed(claims):
        return _reply(request_id, case_id, _say("no_move_rights", lang))
    return _reply(request_id, case_id, "", intent="STAGE_MOVE_CONFIRMED",
                  execute_move={"case_id": case_id, "from": here, "to": there, "ref": str(write["ref"]),
                                "actor": actor})


def moved_text(case_id: str, here: str, there: str, lang: str) -> str:
    return _say("moved", lang, case_id=case_id, here=here, there=there)


def move_failed_text(case_id: str, why: str, lang: str) -> str:
    return _say("move_failed", lang, case_id=case_id, why=why)


def move_unknown_text(case_id: str, there: str, lang: str) -> str:
    return _say("move_unknown", lang, case_id=case_id, there=there)


__all__ = ["KINDS", "enabled", "gate", "move_failed_text", "move_unknown_text", "moved_text", "offers", "problems", "propose", "step"]
