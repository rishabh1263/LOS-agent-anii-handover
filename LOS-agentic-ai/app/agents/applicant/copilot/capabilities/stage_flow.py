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

KINDS = ("customer_query", "free_query", "move", "stage_query", "reply_query", "resolve_query")
_QID = re.compile(r"\bQRY-[0-9A-F]{6,}\b", re.I)


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

def step(write: dict[str, Any], message: str, claims: dict[str, Any], request_id: str,
         lang: str) -> dict[str, Any] | None:
    """The answer to a pending workflow proposal; None when the message is about something else (proposal dropped)."""
    from app.agents.applicant.copilot.capabilities import case_form, workspace

    ref = str(write.get("ref") or "")
    case_id = str(write.get("case_id") or "")
    if write.get("kind") == "free_query" and not write.get("text"):
        # "raise a query" with no words yet: this message IS the query's text -> the draft, then Confirm
        text = " ".join(str(message or "").split())
        if not text or case_form._only(message, [*case_form._words("cancel_words"), "cancel"], ref):
            write["done"] = True
            return _reply(request_id, case_id, _say("cancelled", lang))
        target = str((_cfg().get("free_query") or {}).get("target_type") or "CUSTOMER")
        write.update(text=text[:950], target_type=target, query_type=_query_type(text))
        answer = _say("free_draft", lang, case_id=case_id, target=_say(f"target_{target.lower()}", lang),
                      query_type=_readable_type(write["query_type"])) + "\n\n> " + write["text"]
        return _reply(request_id, case_id, answer, block=_confirm_links(ref, lang), intent="QUERY_DRAFT")
    if write.get("kind") == "stage_query" and not write.get("text"):
        text = " ".join(str(message or "").split())
        if not text or case_form._only(message, [*case_form._words("cancel_words"), "cancel"], ref):
            write["done"] = True
            return _reply(request_id, case_id, _say("cancelled", lang))
        write["text"] = text[:950]
        answer = _say("stage_query_proposal", lang, case_id=case_id, here=write["here"], there=write["there"]) \
            + "\n\n> " + write["text"]
        return _reply(request_id, case_id, answer, block=_confirm_links(ref, lang))
    if case_form._only(message, [*case_form._words("cancel_words"), "cancel"], ref):
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
    except queries.QueryError as exc:
        audit.record(request_id=request_id, subject=actor, applicant_id=None, case_id=case_id, intent=kind.upper(),
                     tools=[], write=True, status="REFUSED", detail=exc.code)
        return _reply(request_id, case_id, _say("query_failed", lang, why=exc.message))
    return _reply(request_id, case_id, _say("cancelled", lang))


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


__all__ = ["KINDS", "enabled", "gate", "move_failed_text", "moved_text", "offers", "problems", "propose", "step"]
