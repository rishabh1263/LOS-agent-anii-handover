"""
THE CASE FORM -- ONE definition for the UI and the chat (MASTER SPEC section 15.5; config product_flow.yaml
`case_form`, flag COPILOT_CHAT_CASE_CREATE).

    FIELDS      the create-case request model (fos_api.ApplicantDetails / ApplicationDetails): names, types, max
                lengths. Config adds the order, the required set, format rules, choices and the words.
    VALIDATE    `check(field, raw)` -- the same rule for the UI create / edit routes and every chat answer; the
                amount rule is the existing plausibility check (applicant_agent.yaml chatbot.plausibility).
    CHAT CREATE "Create new case" -> the required fields one by one -> a summary table -> "Confirm?" -> created
                by the SAME route function as the UI form (permissions, ownership grant, audit). Cancel any time.
    CHAT EDIT   inside an open FOS case: "loan amount 600000 karo" -> the proposal -> "Confirm?" -> saved, audited,
                and written to the case's activity log. Every write asks first; nothing is saved without Confirm.

The pending write lives in the workspace's conversation record (`state.flow["write"]`), per chat.
"""

from __future__ import annotations

import dataclasses
import os
import re
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

FLAG = "COPILOT_CHAT_CASE_CREATE"

APPLICANT_FIELDS = ("full_name", "mobile", "email", "date_of_birth", "address")
APPLICATION_FIELDS = ("product", "loan_amount", "employment_type", "tenure_months", "interest_rate_pct",
                      "declared_monthly_income", "declared_monthly_obligations", "property_value")


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.cfg().get("case_form") or {}


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(_cfg().get("chat_create", False))


def _lang() -> str:
    from app.agents.applicant.copilot.answering import language_lock

    return language_lock.current() or "en"


def _say(key: str, lang: str | None = None, **values: Any) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.say(_cfg().get(key), lang or _lang(), **values)


def label(field: str, lang: str | None = None) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    found = (_cfg().get("labels") or {}).get(field)
    return product_flow.say(found, lang or _lang()) if found else field.replace("_", " ").capitalize()


# --------------------------------------------------------------------------
# the schema (from the request model) and the rules (from config)
# --------------------------------------------------------------------------

def _models():
    from app.api.routes.fos_api import ApplicantDetails, ApplicationDetails

    return ApplicantDetails, ApplicationDetails


def _max_length(field: str) -> int | None:
    applicant, application = _models()
    model = applicant if field in APPLICANT_FIELDS else application
    info = model.model_fields.get(field)
    for meta in getattr(info, "metadata", []) or []:
        if getattr(meta, "max_length", None):
            return int(meta.max_length)
    return None


def choices(field: str) -> list[str]:
    if field == "product":
        from app.agents.applicant import config

        return [p for p in config.products() if str(p).lower() != "default"]
    return [str(c) for c in (_cfg().get("choices") or {}).get(field) or []]


def schema(lang: str | None = None) -> dict[str, Any]:
    """The form for the UI (GET /api/v1/fos/form-schema): every field, in order, with its rules and words."""
    lang = lang or _lang()
    order = list(_cfg().get("order") or (*APPLICANT_FIELDS, *APPLICATION_FIELDS))
    required = set(_cfg().get("required") or [])
    formats = _cfg().get("formats") or {}
    return {"fields": [{"name": f, "group": "applicant" if f in APPLICANT_FIELDS else "application",
                        "label": label(f, lang), "required": f in required, "max_length": _max_length(f),
                        "choices": choices(f) or None, "rules": formats.get(f) or {}} for f in order]}


def _why(key: str, lang: str, **values: Any) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.say((_cfg().get("why") or {}).get(key) or key, lang, **values)


def check(field: str, raw: Any, lang: str | None = None) -> tuple[Any, str | None]:
    """(the clean value, None) or (None, why it is not valid) -- one rule for the UI and the chat."""
    lang = lang or _lang()
    text = "" if raw is None else " ".join(str(raw).split())
    if not text:
        return None, (_why("required", lang) if field in (_cfg().get("required") or []) else None)
    limit = _max_length(field)
    if limit and len(text) > limit:
        return None, _why("too_long", lang, max=limit)
    rule = (_cfg().get("formats") or {}).get(field) or {}
    if rule.get("strip"):
        text = re.sub(rule["strip"], "", text)
    if field in ("product", "employment_type"):
        wanted = text.upper().replace(" ", "_").replace("-", "_")
        options = choices(field)
        if options and wanted not in options:
            return None, _why("choice", lang, choices=", ".join(shown(field, o) for o in options))
        return wanted, None
    if rule.get("date"):
        try:
            born = date.fromisoformat(text)
        except ValueError:
            return None, _why("date", lang)
        today = date.today()
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        low, high = int(rule.get("min_age", 0)), int(rule.get("max_age", 150))
        if not low <= age <= high:
            return None, _why("age", lang, min=low, max=high)
        return born.isoformat(), None
    if rule.get("integer") or rule.get("number"):
        from app.agents.applicant import plausibility

        found = plausibility.problem({field: text})
        if found is not None:
            return None, found[1]
        try:
            number = float(re.sub(r"[,\s₹]|rs\.?|inr", "", text.lower()))
        except ValueError:
            return None, _why("number", lang)
        if rule.get("integer") and number != int(number):
            return None, _why("number", lang)
        if ("min" in rule and number < float(rule["min"])) or ("max" in rule and number > float(rule["max"])):
            return None, _why("range", lang, min=rule.get("min"), max=rule.get("max"))
        return (int(number) if rule.get("integer") or number == int(number) else number), None
    if rule.get("pattern") and not re.fullmatch(rule["pattern"], text):
        return None, _why("mobile" if field == "mobile" else "format", lang)
    return text, None


def check_all(values: dict[str, Any], lang: str | None = None, *, partial: bool = False) -> dict[str, str]:
    """{field: why} for every invalid field (partial: an absent field is fine -- an edit, a draft)."""
    errors: dict[str, str] = {}
    for field in (*APPLICANT_FIELDS, *APPLICATION_FIELDS):
        if partial and field not in values:
            continue
        _, why = check(field, values.get(field), lang)
        if why:
            errors[field] = why
    return errors


def values_of(case_id: str) -> dict[str, Any]:
    """The form as saved (the UI's "continue where you stopped"). The caller has authorized the case."""
    from app.store import get_repository

    repo = get_repository()
    application = repo.get_application(case_id)
    if application is None:
        return {}
    applicant = repo.get_applicant(application.applicant_id)
    out = {f: getattr(applicant, f, None) for f in APPLICANT_FIELDS} if applicant else {}
    out |= {f: getattr(application, f, None) for f in APPLICATION_FIELDS}
    return out


def missing(case_id: str) -> list[str]:
    values = values_of(case_id)
    return [f for f in _cfg().get("required") or [] if values.get(f) in (None, "")]


def editable(case_id: str) -> bool:
    from app.agents.applicant.copilot.capabilities import workspace

    return str(workspace._stage(case_id) or "").upper() in {s.upper() for s in _cfg().get("editable_stages") or []}


def save(case_id: str, changes: dict[str, Any], claims: dict[str, Any], request_id: str, channel: str) -> None:
    """Write already-validated changes to the case (the caller has authorized it), audit + activity log."""
    from app.agents.applicant import audit
    from app.security.auth import get_subject
    from app.store import get_repository

    repo = get_repository()
    application = repo.get_application(case_id)
    applicant = repo.get_applicant(application.applicant_id)
    now = datetime.now(timezone.utc)
    to_applicant = {k: v for k, v in changes.items() if k in APPLICANT_FIELDS}
    to_application = {k: (str(v) if v is not None else None) for k, v in changes.items() if k in APPLICATION_FIELDS}
    if to_applicant:
        repo.save_applicant(dataclasses.replace(applicant, **to_applicant, updated_at=now))
    if to_application:
        repo.save_application(dataclasses.replace(application, **to_application, updated_at=now))
    subject = str(get_subject(claims) or "")
    audit.record(request_id=request_id, subject=subject, applicant_id=application.applicant_id, case_id=case_id,
                 intent="UPDATE_CASE_FORM", tools=["application.update"], write=True, confirmed=True, status="OK",
                 detail=f"channel={channel} fields={','.join(sorted(changes))}")
    activity(case_id, "FORM_UPDATED", f"{', '.join(sorted(changes))} updated by {subject} ({channel})")


def activity(case_id: str, kind: str, summary: str, party_id: str | None = None) -> None:
    """One activity-log entry (the case timeline). Never fails the write it describes."""
    from app.agents.applicant.copilot.capabilities import abuse_guard
    from app.security import sensitivity
    from app.store import get_repository
    from app.store.models import CaseEvent

    try:
        get_repository().record_event(CaseEvent(event_id=f"evt_{uuid.uuid4().hex}", case_id=case_id, event_type=kind,
                                                party_id=party_id,
                                                summary=abuse_guard.mask_text(sensitivity.mask_identifiers(summary))))
    except Exception:  # noqa: BLE001 - the log describes the write; it never undoes it
        pass


# --------------------------------------------------------------------------
# the chat: create (collect -> summary -> Confirm) and edit (propose -> Confirm)
# --------------------------------------------------------------------------

def _words(key: str) -> list[str]:
    return [str(w) for w in _cfg().get(key) or []]


def _norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", re.sub(r"[^\w\sऀ-ॿ-]", " ", str(text or "").lower())).strip() + " "


def _only(text: str, words: list[str], ref: str | None = None) -> bool:
    said = _norm(text).strip()
    if ref and said.endswith(" " + ref.lower()):
        said = said[: -len(ref) - 1].strip()
    return said in {_norm(w).strip() for w in words}


def _reply(request_id: str, answer: str, *, intent: str = "CASE_FORM", case_id: str | None = None,
           **extra: Any) -> dict[str, Any]:
    return {"request_id": request_id, "intent": intent, "answer": answer, "case_id": case_id,
            "category": "CASE_ONLY", "query_type": "CONVERSATION", "response_source": "STRUCTURED", "documents": [],
            "actions": [], "errors": [], "tools_invoked": [], "suggested_questions": [], **extra}


def _ask(field: str, lang: str) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    hint = (_cfg().get("hints") or {}).get(field)
    hint_text = product_flow.say(hint, lang, choices=", ".join(shown(field, c) for c in choices(field))) if hint else ""
    if field not in (_cfg().get("required") or []):
        hint_text += product_flow.say(_cfg().get("optional_hint"), lang)
    return _say("ask", lang, label=label(field, lang), hint=hint_text)


def shown(field: str, value: Any) -> str:
    """A value as the officer reads it: 500000.0 -> 500000, PERSONAL_LOAN -> Personal loan (stored unchanged)."""
    if value is None:
        return ""
    text = str(value)
    if re.fullmatch(r"-?\d+\.0+", text):
        return text.split(".")[0]
    if field in ("product", "employment_type") and re.fullmatch(r"[A-Z_]+", text):
        return text.replace("_", " ").capitalize()
    return text


def _next(values: dict[str, Any]) -> str | None:
    return next((f for f in _cfg().get("required") or [] if values.get(f) in (None, "")), None)


def _summary(values: dict[str, Any], lang: str, ref: str) -> str:
    from app.agents.applicant.copilot.answering import contract

    rows = [f"| {label(f, lang)} | {shown(f, values[f]).replace('|', '/')} |"
            for f in _cfg().get("order") or [] if values.get(f) not in (None, "")]
    optional = [f for f in _cfg().get("order") or [] if values.get(f) in (None, "")
                and f not in (_cfg().get("required") or [])]
    head = "| " + " | ".join(_field_columns(lang)) + " |"
    edits = " · ".join(contract.ask(_say("edit_label", lang, label=label(f, lang)))
                       for f in [*(_cfg().get("required") or []), *optional][:8])
    return "\n".join([_say("summary", lang), "", head, "|---|---|", *rows, "", _say("confirm_q", lang),
                      contract.link("confirm_write", lang, ref=ref) + " · " + contract.link("cancel_write", lang,
                                                                                            ref=ref),
                      "", edits])


def _field_columns(lang: str) -> list[str]:
    from app.agents.applicant.copilot.answering import language_lock
    from app.agents.applicant.copilot.capabilities import product_flow

    cols = language_lock.pick((product_flow.cfg().get("exports") or {}).get("field_columns"), lang)
    return list(cols) if isinstance(cols, list) else ["Field", "Value"]


def _edit_target(text: str, lang: str) -> str | None:
    """'Edit Mobile' / 'mobile badlo' -> the field the officer wants to change (labels in every language)."""
    from app.agents.applicant.copilot.answering import language_lock

    said = _norm(text)
    for field in _cfg().get("order") or []:
        names = {field.replace("_", " ")}
        raw = (_cfg().get("labels") or {}).get(field) or {}
        names |= {re.sub(r"\(.*?\)", "", str(v)).strip().lower() for v in (raw.values() if isinstance(raw, dict)
                                                                          else [raw])}
        names |= {re.sub(r"\(.*?\)", "", str(language_lock.pick(raw, lang))).strip().lower()} if raw else set()
        if any(n and _norm(n) in said for n in names):
            return field
    return None


def _stage_flow():
    from app.agents.applicant.copilot.capabilities import stage_flow

    return stage_flow


def _escapes(message: str, write: dict[str, Any]) -> bool:
    """
    A message that is clearly NOT the asked value: a question ("?"), one starting with a question / command word,
    or naming cases / KYC / downloads (product_flow.yaml case_form.escape_*). Confirm / Cancel / Edit never escape.
    """
    ref = str(write.get("ref") or "draft")
    if any(_only(message, _words(k), ref) for k in ("confirm_words", "cancel_words")) \
            or _only(message, ["confirm", "cancel"], ref) \
            or any(_norm(w) in _norm(message) for w in _words("edit_words")):
        return False
    words = _norm(message).split()
    if not words:
        return False
    return "?" in str(message) or words[0] in {_norm(w).strip() for w in _words("escape_first")} \
        or bool(set(words) & {_norm(w).strip() for w in _words("escape_any")})


Creator = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def chat_turn(action: str, message: str, claims: dict[str, Any], request_id: str,
                    context: dict[str, Any] | None, create: Creator, wants_new_case: bool) -> dict[str, Any] | None:
    """
    This turn's reply when it belongs to a case draft or a field edit; else None (the message goes on as usual).
    `create` is the UI form's own create function (same permissions, grant, audit); `wants_new_case` -- the
    message / button asks to start a new case.
    """
    from app.agents.applicant.copilot.capabilities import workspace

    if not enabled():
        return None
    lang = _lang()
    imported = re.fullmatch(r"\s*(confirm|cancel)\s+(imp_[0-9a-f]{6,})\s*", str(message or ""), re.I)
    if imported:
        return await _import_step(imported.group(1).lower(), imported.group(2), claims, request_id, lang, create)
    workspace_id = (context or {}).get("workspace_id") if isinstance(context, dict) else None
    state = workspace._state(workspace._subject(claims), workspace_id)
    flow = dict(state.flow or {})
    write = flow.get("write") if isinstance(flow.get("write"), dict) else None
    if write and float(write.get("expires", 0)) < time.time():
        write = None
    reply = None
    if write and write.get("kind") == "create" and (write.get("parked") or wants_new_case):
        ref = str(write.get("ref") or "draft")
        decisive = _only(message, [*_words("confirm_words"), "confirm"], ref) \
            or _only(message, [*_words("cancel_words"), "cancel"], ref)
        if decisive:                                        # Confirm / Cancel always reach the draft
            write.pop("parked", None)
            reply = await _create_step(write, message, claims, request_id, lang, create, state)
        elif wants_new_case or _only(message, _words("resume_words")):
            write.pop("parked", None)                       # "Create New Case" again: continue, never restart
            follow = _ask(write["field"], lang) if write.get("field") else _summary(write.get("values") or {}, lang,
                                                                                     ref)
            reply = _reply(request_id, _say("resume", lang) + "\n\n" + follow)
        else:
            return None                                     # parked: the message is answered as usual
    elif write and write.get("kind") == "create" and _escapes(message, write):
        write["parked"] = True                              # a question / command: answered as usual, nothing lost
    elif write and write.get("kind") == "create":
        reply = await _create_step(write, message, claims, request_id, lang, create, state)
    elif write and write.get("kind") == "edit":
        reply = _edit_step(write, message, claims, request_id, lang)
        if reply is None:
            write = None                                    # anything else: the proposal is dropped
    elif write and write.get("kind") in _stage_flow().KINDS:
        # the FOS <-> CPA workflow (customer query, move to CPA, stage queries): Confirm / Cancel / the text
        reply = _stage_flow().step(write, message, claims, request_id, lang)
        if reply is None:
            write = None
    elif wants_new_case:
        from app.agents.applicant import permissions
        from app.agents.applicant.copilot.semantics.intents import Intent
        from app.agents.applicant.permissions import Caller, PermissionDenied

        try:
            permissions.check_capability(Caller.from_claims(claims), Intent.CREATE_APPLICANT)
        except PermissionDenied:
            return _reply(request_id, _say("no_rights", lang))
        from app.agents.applicant.copilot.answering import contract

        write = {"kind": "create", "values": {}, "field": _next({}), "ref": "draft",
                 "expires": time.time() + float(_cfg().get("draft_ttl_seconds", 1800))}
        reply = _reply(request_id, "\n\n".join([_say("start", lang), _ask(write["field"], lang),
                                                _say("or_form", lang) + " " + contract.link("new_case", lang)]))
    elif action == "CUSTOM_QUERY" and state.active_case_id:
        reply, write = _stage_flow().propose(message, state.active_case_id, claims, request_id, lang)
        if reply is None:
            reply, write = _propose_edit(message, state.active_case_id, claims, request_id, lang)
    if reply is None and write is None and not flow.get("write"):
        return None
    flow["write"] = write if write and not write.get("done") else None
    if flow["write"] is None:
        flow.pop("write")
    state.flow = flow
    workspace._save(state)
    return reply


async def _import_step(verb: str, import_id: str, claims: dict[str, Any], request_id: str, lang: str,
                       create: Creator) -> dict[str, Any]:
    """Confirm / Cancel of a sheet validated in this chat: the valid rows through the one create path."""
    from fastapi import HTTPException

    from app.agents.applicant.copilot.capabilities import importer, product_flow, workspace

    rows = importer.take(import_id, workspace._subject(claims))
    texts = (product_flow.cfg().get("import") or {}).get("texts") or {}
    if rows is None:
        return _reply(request_id, product_flow.say(texts.get("expired"), lang))
    if verb == "cancel":
        return _reply(request_id, _say("cancelled", lang))
    made = []
    for values in rows:
        try:
            created = await create(importer.body_for(values))
            created = created.model_dump() if hasattr(created, "model_dump") else dict(created or {})
            made.append(str(created.get("case_id") or ""))
        except HTTPException:
            continue
    return _reply(request_id, product_flow.say(texts.get("done"), lang, n=len(made)) + "\n"
                  + "\n".join(f"- {c}" for c in made if c), intent="CASE_IMPORTED",
                  tools_invoked=["applicant.create", "application.create"])


async def _create_step(write: dict[str, Any], message: str, claims: dict[str, Any], request_id: str, lang: str,
                       create: Creator, state) -> dict[str, Any]:
    values = write.setdefault("values", {})
    ref = str(write.get("ref") or "draft")
    if _only(message, _words("cancel_words"), ref) or _only(message, ["cancel"], ref):
        write["done"] = True
        return _reply(request_id, _say("cancelled", lang))
    field = write.get("field")
    if field is None:
        # the summary is on screen: Confirm, Cancel or Edit <field>
        if _only(message, _words("confirm_words"), ref) or _only(message, ["confirm"], ref):
            return await _create_now(write, claims, request_id, lang, create, state)
        target = _edit_target(message, lang)
        if target:
            write["field"], write["editing"] = target, True
            return _reply(request_id, _ask(target, lang))
        return _reply(request_id, _summary(values, lang, ref))
    if _only(message, _words("skip_words")) and field not in (_cfg().get("required") or []):
        values.pop(field, None)
        value, why = None, None
    else:
        value, why = check(field, message, lang)
    if why:
        return _reply(request_id, _say("invalid", lang, label=label(field, lang), why=why) + "\n\n" + _ask(field, lang))
    if value is not None:
        values[field] = value
    write["field"] = None if write.pop("editing", False) else _next(values)
    if write["field"]:
        return _reply(request_id, _ask(write["field"], lang))
    return _reply(request_id, _summary(values, lang, ref))


async def _create_now(write: dict[str, Any], claims: dict[str, Any], request_id: str, lang: str, create: Creator,
                      state) -> dict[str, Any]:
    from fastapi import HTTPException

    values = write.get("values") or {}
    errors = check_all(values, lang)
    if errors:
        field = next(iter(errors))
        write["field"], write["editing"] = field, True
        return _reply(request_id, _say("invalid", lang, label=label(field, lang), why=errors[field]) + "\n\n"
                      + _ask(field, lang))
    body = {"applicant": {f: values[f] for f in APPLICANT_FIELDS if values.get(f) not in (None, "")},
            "application": {f: values[f] for f in APPLICATION_FIELDS if values.get(f) not in (None, "")}}
    try:
        created = await create(body)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        write["field"] = detail.get("field") if detail.get("field") in values else None
        return _reply(request_id, _say("failed", lang, why=str(detail.get("message") or "")))
    created = created.model_dump() if hasattr(created, "model_dump") else dict(created or {})
    case_id = str(created.get("case_id") or "")
    write["done"] = True
    state.active_case_id = case_id or state.active_case_id          # the new case is the open case now
    required = sum(1 for item in created.get("checklist") or [] if isinstance(item, dict) and item.get("mandatory"))
    return _reply(request_id, _say("created", lang, case_id=case_id, name=values.get("full_name") or "",
                                   n=required), intent="CASE_CREATED", case_id=case_id,
                  applicant_id=created.get("applicant_id"), tools_invoked=["applicant.create", "application.create"])


def _propose_edit(message: str, case_id: str, claims: dict[str, Any], request_id: str,
                  lang: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """'loan amount 600000 karo' inside the open case -> the proposal + Confirm; else (None, None)."""
    said = _norm(message)
    if not any(_norm(w) in said for w in _words("edit_cues")):
        return None, None
    field = _edit_target(message, lang)
    if field is None:
        return None, None
    raw = _value_in(message, field, lang)
    if not raw:
        return None, None
    from app.agents.applicant.copilot.capabilities import workspace

    workspace.authorize(claims, case_id)
    if not editable(case_id):
        return _reply(request_id, _say("edit_locked", lang), case_id=case_id), None
    value, why = check(field, raw, lang)
    if why:
        return _reply(request_id, _say("invalid", lang, label=label(field, lang), why=why), case_id=case_id), None
    from app.agents.applicant.copilot.answering import contract

    old = values_of(case_id).get(field)
    ref = f"edit-{uuid.uuid4().hex[:6]}"
    answer = _say("edit_proposal", lang, label=label(field, lang), case_id=case_id,
                  old=shown(field, old) if old not in (None, "") else _say("empty", lang),
                  new=shown(field, value)) + "\n\n" + \
        contract.link("confirm_write", lang, ref=ref) + " · " + contract.link("cancel_write", lang, ref=ref)
    write = {"kind": "edit", "case_id": case_id, "field": field, "value": value, "ref": ref,
             "expires": time.time() + float(_cfg().get("draft_ttl_seconds", 1800))}
    return _reply(request_id, answer, case_id=case_id), write


def _value_in(message: str, field: str, lang: str) -> str | None:
    """The new value: what follows the field's name (and "to" / "ko" / ":"), without the trailing command word."""
    text = " ".join(str(message).split())
    low = text.lower()
    raw = (_cfg().get("labels") or {}).get(field) or {}
    names = sorted({field.replace("_", " ")} | {re.sub(r"\(.*?\)", "", str(v)).strip().lower()
                                                 for v in (raw.values() if isinstance(raw, dict) else [raw])},
                   key=len, reverse=True)
    for name in names:
        at = low.find(name)
        if name and at >= 0:
            rest = text[at + len(name):]
            break
    else:
        return None
    rest = re.sub(r"^\s*(?:to|ko|=|:|-|ka|ki|ke|se)\s+", " ", rest, flags=re.I).strip(" :=-")
    cues = sorted(_words("edit_cues"), key=len, reverse=True)
    for cue in cues:
        rest = re.sub(rf"(?i)\s*\b{re.escape(cue)}\b\s*$", "", rest).strip()
        rest = re.sub(rf"(?i)^\s*{re.escape(cue)}\b\s*", "", rest).strip()
    rest = re.sub(r"(?i)^(?:to|ko)\s+", "", rest).strip()
    return rest or None


def _edit_step(write: dict[str, Any], message: str, claims: dict[str, Any], request_id: str,
               lang: str) -> dict[str, Any] | None:
    ref = str(write.get("ref") or "")
    case_id = str(write.get("case_id") or "")
    if _only(message, _words("cancel_words"), ref) or _only(message, ["cancel"], ref):
        write["done"] = True
        return _reply(request_id, _say("edit_cancelled", lang), case_id=case_id)
    if not (_only(message, _words("confirm_words"), ref) or _only(message, ["confirm"], ref)):
        return None
    from app.agents.applicant.copilot.capabilities import workspace

    workspace.authorize(claims, case_id)                       # scope re-checked at the write
    if not editable(case_id):
        write["done"] = True
        return _reply(request_id, _say("edit_locked", lang), case_id=case_id)
    field = str(write["field"])
    value, why = check(field, write.get("value"), lang)        # re-validated at the write
    if why:
        write["done"] = True
        return _reply(request_id, _say("invalid", lang, label=label(field, lang), why=why), case_id=case_id)
    save(case_id, {field: value}, claims, request_id, "chat")
    write["done"] = True
    left = missing(case_id)
    answer = _say("edited", lang, label=label(field, lang), case_id=case_id, new=shown(field, value))
    if left:
        answer += "\n\n" + _say("missing_fields", lang, fields=", ".join(label(f, lang) for f in left))
    return _reply(request_id, answer, intent="CASE_FORM_UPDATED", case_id=case_id,
                  tools_invoked=["application.update"])


__all__ = ["APPLICANT_FIELDS", "APPLICATION_FIELDS", "FLAG", "activity", "chat_turn", "check", "check_all",
           "choices", "editable", "enabled", "label", "missing", "save", "schema", "values_of"]
