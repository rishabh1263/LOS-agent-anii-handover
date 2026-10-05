"""
THE ANSWER IN THE USER'S LANGUAGE -- for the facts a deterministic template
exists for (app/config/languages.yaml `templates`), on the FOS surface.

  current stage      "तुमचा अर्ज सध्या FOS (Basic Document Verification) टप्प्यात आहे."
  pending documents  the SAME two lists the English answer is built from

ONLY RECORDED VALUES are filled in: the case's stage label and recorded
status, the pending slots and documents the English answer named. A fact with
no template stays in English, and the language contract says so
(`reply_language: en`, `localized: false`) -- the frontend never has to guess
which language the prose is in. No model writes or translates here.

The Universal Copilot localizes in its own presentation layer
(copilot_api._converse); this module is the FOS surface's equivalent, over
the same templates.
"""

from __future__ import annotations

import re
from typing import Any


def _current_stage_sentence(result: dict[str, Any], message: str, language: str) -> str | None:
    from app.agents.applicant import config as agent_config
    from app.agents.applicant import language as languages
    from app.agents.applicant import normalize
    from app.agents.applicant.copilot.answering.answer import _readable
    from app.agents.applicant.copilot.semantics import intents

    said = normalize.normalise(message).text or message
    matched = str(intents.understand(message, has_case=True).matched_on or "")
    if not (intents.asks_current_stage(said) or matched.startswith("frame:CURRENT_STAGE")) \
            or intents.stage_in(said):
        return None
    understanding = result.get("understanding") if isinstance(result.get("understanding"), dict) else {}
    case_stage = (understanding or {}).get("case_stage")
    if not case_stage:
        return None
    label = agent_config.stage_label(str(case_stage))
    status = result.get("stage")
    if isinstance(status, str) and status and status.upper() != str(case_stage).upper():
        label = f"{label} ({_readable(status)})"
    return languages.localized("current_stage", language, stage=label)


def _pending_sentence(result: dict[str, Any], language: str) -> str | None:
    from app.agents.applicant import language as languages
    from app.agents.applicant.copilot.answering import answer as answers

    if (result.get("subject") or {}).get("parties") if isinstance(result.get("subject"), dict) else False:
        return None                      # a two-person answer has no single-list template
    missing = [answers._readable(i.get("slot")) for i in result.get("pending_items") or []
               if isinstance(i, dict) and i.get("code") == "DOCUMENT_MISSING"]
    awaiting = [answers._readable(d.get("document_type")) for d in result.get("documents") or []
                if isinstance(d, dict) and d.get("status") in {"UPLOADED", "PROCESSING", "REVIEW"}]
    return languages.localized_pending(language, missing, awaiting)


def _listed(items: list[str]) -> str:
    return ", ".join(dict.fromkeys(i for i in items if i))


def _gate_sentence(result: dict[str, Any], language: str) -> str | None:
    """A stage gate, from the gate block: ready, or what is still open."""
    from app.agents.applicant import config as agent_config
    from app.agents.applicant import language as languages

    gate = result.get("gate")
    if not isinstance(gate, dict) or gate.get("moved_to") or gate.get("notes") \
            or gate.get("status") == "CONFIGURATION_GAP":
        return None
    stage = agent_config.stage_label(gate.get("stage")) if gate.get("stage") else None
    nxt = agent_config.stage_label(gate.get("next_stage")) if gate.get("next_stage") else None
    if not (stage and nxt):
        return None
    if gate.get("status") == "PASS":
        return languages.localized("gate_ready", language, stage=stage, next=nxt)
    from app.agents.applicant.copilot.answering.answer import _readable

    items = []
    for check in gate.get("blockers") or []:
        # the RECORDED slot or code, never the English sentence around it
        evidence = [_readable(str(e.get("slot") or e.get("code")))
                    for e in check.get("evidence") or []
                    if isinstance(e, dict) and (e.get("slot") or e.get("code"))] \
            if check.get("id") == "FOS_READINESS" else []
        items += evidence or [str(check.get("label"))]
    return languages.localized("gate_blocked", language, stage=stage, next=nxt, items=_listed(items)) \
        if items else None


def _readiness_sentence(result: dict[str, Any], language: str) -> str | None:
    """The FOS readiness verdict (the FOS -> CPA gate) from the readiness block --
    only at FOS, and never for a two-party answer whose nuance a template drops."""
    from app.agents.applicant import config as agent_config
    from app.agents.applicant import language as languages
    from app.agents.applicant.copilot.answering.answer import _readable

    readiness = result.get("readiness")
    understanding = result.get("understanding") if isinstance(result.get("understanding"), dict) else {}
    subject = result.get("subject") if isinstance(result.get("subject"), dict) else {}
    if not isinstance(readiness, dict) or (understanding or {}).get("case_stage") not in (None, "FOS") \
            or (subject or {}).get("parties"):
        return None
    stage, nxt = agent_config.stage_label("FOS"), agent_config.stage_label("CPA")
    if readiness.get("status") == "READY_FOR_CPA":
        return languages.localized("gate_ready", language, stage=stage, next=nxt)
    items = [_readable(str(i.get("slot") or i.get("code"))) for i in readiness.get("blocking_items") or []
             if isinstance(i, dict) and (i.get("slot") or i.get("code"))]
    return languages.localized("gate_blocked", language, stage=stage, next=nxt,
                               items=_listed(items)) if items else None


def _work_sentence(result: dict[str, Any], language: str) -> str | None:
    """Pending work, from the pending-work block: what was verified, what is
    running, what needs the person, what waits on a reviewer."""
    from app.agents.applicant import language as languages

    work = result.get("pending_work")
    if not isinstance(work, dict):
        return None
    items = work.get("items") or []
    executed = {str(e.get("document_id")): e.get("result") for e in work.get("executed") or []}

    def labels(pred) -> str:
        return _listed([str(i.get("label")) for i in items if pred(i)])

    parts = []
    verified = labels(lambda i: executed.get(str(i.get("document_id"))) == "VERIFIED_NOW")
    running = labels(lambda i: i.get("owner") == "PROCESSING")
    upload = labels(lambda i: i.get("owner") == "USER")
    review = labels(lambda i: i.get("owner") == "REVIEWER")
    # SYSTEM work the session may not start: said, never silently dropped
    blocked = labels(lambda i: i.get("owner") == "SYSTEM" and not i.get("executable"))
    for fact, value in (("work_verified", verified), ("work_processing", running),
                        ("work_no_permission", blocked), ("work_upload", upload), ("work_review", review)):
        if value:
            sentence = languages.localized(fact, language, items=value)
            if sentence is None:
                return None                 # a missing template: the English answer stands
            parts.append(sentence)
    if not parts and not any(i.get("owner") == "SYSTEM" for i in items):
        return languages.localized("work_nothing", language)
    return " ".join(parts) or None


def _kyc_sentence(result: dict[str, Any], language: str) -> str | None:
    """Each person's RECORDED KYC status (and reason codes), from the kyc block.
    Any part without a template keeps the whole answer in English."""
    from app.agents.applicant import language as languages

    kyc = result.get("kyc")
    parties = kyc.get("parties") if isinstance(kyc, dict) else None
    if not parties:
        return None
    sentences = []
    for party in parties:
        if not isinstance(party, dict):
            return None
        who = languages.localized("kyc_who_co" if party.get("party_role") == "CO_APPLICANT"
                                  else "kyc_who_self", language)
        said = languages.localized(f"kyc_{party.get('status')}", language, who=who or "")
        if not who or not said:
            return None
        if party.get("status") != "PASS" and party.get("reason_codes"):
            reasons = [languages.localized(f"kyc_reason_{code}", language)
                       for code in party.get("reason_codes") or []]
            because = languages.localized("kyc_because", language, reason=", ".join(r or "" for r in reasons))
            if not all(reasons) or not because:
                return None
            said = f"{said} {because}"
        sentences.append(said)
    extras = _kyc_extras(result, language, parties)
    if extras is None:
        return None
    return " ".join(sentences + ([extras] if extras else []))


#: "the PAN says A, but the bank statement says B" -- the values the KYC
#: answer quoted, copied verbatim into the localized one
_SAYS = re.compile(
    r"\bthe ([A-Za-z][\w' -]{1,40}?) says ([^,]+?), but the ([A-Za-z][\w' -]{1,40}?) says ([^.]+?)\.")
#: the case's hold, appended by the agent after a document answer
_CASE_HOLD = re.compile(
    r"However, (?:your|the|the customer's) application (is under review|was declined) because (.+?)\.\s*$")


def _end(language: str) -> str:
    from app.agents.applicant import language as languages

    return languages.localized("sentence_end", language) or "."


def _kyc_extras(result: dict[str, Any], language: str, parties: list[dict[str, Any]]) -> str | None:
    """The score and the quoted values the English KYC answer carried; None
    when one of them has no template (the English answer then stands)."""
    from app.agents.applicant import language as languages

    extra = []
    said = _SAYS.search(str(result.get("answer_en") or result.get("answer") or ""))
    if said:
        values = languages.localized("kyc_values", language, doc_a=said.group(1), value_a=said.group(2),
                                     doc_b=said.group(3), value_b=said.group(4))
        if not values:
            return None
        extra.append(values)
    scores = [p.get("score") for p in parties if p.get("score_recorded") and p.get("score") is not None]
    if len(scores) == 1:
        score = languages.localized("kyc_score", language, score=str(scores[0]))
        if not score:
            return None
        extra.append(score)
    return " ".join(extra)


def _case_hold(english: str, language: str) -> str | None:
    """The agent's "However, the application is under review because ..."
    sentence; its reason is the recorded clause, quoted. "" when absent."""
    from app.agents.applicant import language as languages

    hold = _CASE_HOLD.search(english or "")
    if not hold:
        return ""
    fact = "case_declined" if hold.group(1) == "was declined" else "case_under_review"
    return languages.localized(fact, language, reason=hold.group(2))


def _verification_sentence(result: dict[str, Any], language: str) -> str | None:
    """Each document's recorded verification state and score, from the
    verification block; any part without a template keeps the English."""
    from app.agents.applicant import language as languages
    from app.agents.applicant.copilot.answering import answer as answers

    block = result.get("verification")
    documents = block.get("documents") if isinstance(block, dict) else None
    if not documents:
        return None
    sentences = []
    for d in documents:
        if not isinstance(d, dict):
            return None
        label = str(d.get("label") or answers._readable(d.get("document_type")))
        if str(d.get("party_role") or "").upper() == "CO_APPLICANT":
            label = languages.localized("doc_of_co", language, document=label) or ""
        said = languages.localized(f"doc_{str(d.get('status') or '').upper()}", language, document=label)
        if not label or not said:
            return None
        if d.get("score_recorded") and d.get("score") is not None:
            fact = "doc_score_confidence" if d.get("confidence") is not None else "doc_score"
            score = languages.localized(fact, language, score=str(d.get("score")),
                                        confidence=str(d.get("confidence")))
            if score is None:
                return None
            said += score
        sentences.append(said + _end(language))
    english = str(result.get("answer") or "")
    if answers.INTEGRITY_ONLY.split(";")[0] in english:
        note = languages.localized("doc_integrity_only", language)
        if not note:
            return None
        sentences.append(note)
    hold = _case_hold(english, language)
    if hold is None:
        return None
    if hold:
        sentences.append(hold)
    return " ".join(sentences)


def _next_sentence(result: dict[str, Any], language: str) -> str | None:
    """
    The next step, rebuilt from the SAME next-best-action result the English
    answer was built from (actions.answer), with the configured action
    phrases of `language`. Only when the rebuilt English matches the answer
    given -- a delay or blocking explanation stays in English.
    """
    from app.agents.applicant import actions
    from app.agents.applicant import language as languages
    from app.agents.applicant import workflow
    from app.agents.applicant.copilot.answering import answer as answers

    nba = result.get("_nba_internal")
    if not isinstance(nba, dict) or not isinstance(nba.get("primary"), dict) or result.get("delay"):
        return None
    table = ((actions._config().get("phrases") or {}).get(language) or {}).get("action") or {}
    next_action = result.get("next_action") if isinstance(result.get("next_action"), dict) else {}
    primary = nba["primary"]
    from_workflow = primary.get("source_rule") == "workflow.next_action"
    parties = {str((a.get("subject") or {}).get("party_role")) for a in [primary, *(nba.get("additional") or [])]}
    multi = len(parties - {"None"}) > 1
    # THE ANSWER GIVEN MUST BE THIS ONE: every step it is rebuilt from is in
    # the English text (a delay or blocking explanation words them otherwise)
    given = str(result.get("answer") or "").lower()
    steps = [str(next_action.get("detail") or "").rstrip(".") if from_workflow else actions._phrase(primary)]
    steps += [actions._phrase(a) for a in (nba.get("additional") or [])
              if a.get("action_code") != primary.get("action_code")
              or (a.get("subject") or {}).get("scope") != "CASE"][:2]
    if not all(step and step.lower() in given for step in steps):
        return None

    def phrase(action: dict[str, Any]) -> str | None:
        if action.get("action_code") not in table:
            return None
        return actions._phrase(action, language)

    if from_workflow:
        detail = str(next_action.get("detail") or "")
        code = next((c for c, a, d in workflow._ACTIONS
                     if a == next_action.get("action") and detail.startswith(d.rstrip("."))), None)
        if code is None and next_action.get("action") == "SUBMIT_TO_CPA":
            code = "SUBMIT_TO_CPA"
        target = next_action.get("target")
        first = languages.localized(f"workflow_{code}", language,
                                    document=answers._readable(target) if target else "") if code else None
    else:
        first = phrase(primary)
    if not first:
        return None
    said = languages.localized("next_first", language, action=first)
    extra = [a for a in nba.get("additional") or []
             if a.get("action_code") != primary.get("action_code")
             or (a.get("subject") or {}).get("scope") != "CASE"][:2]
    if extra:
        clauses = []
        for a in extra:
            words = phrase(a)
            if not words:
                return None
            role = (a.get("subject") or {}).get("party_role")
            if multi and role:
                words += languages.localized("next_for_co" if role == "CO_APPLICANT"
                                             else "next_for_primary", language) or ""
            clauses.append(words)
        also = languages.localized("next_also", language,
                                   actions=(languages.localized("next_join", language) or ", ").join(clauses))
        if not also:
            return None
        said = f"{said} {also}"
    return said


def _checklist_sentence(result: dict[str, Any], language: str) -> str | None:
    """Required and optional documents with their states, from the checklist
    block; a provisional checklist (a rule not evaluated) stays in English."""
    from app.agents.applicant import language as languages
    from app.agents.applicant.copilot.answering import answer as answers

    checklist = result.get("checklist")
    if not isinstance(checklist, list) or not checklist:
        return None
    join = languages.localized("next_join", language) or ", "
    provisional = ""
    if isinstance(result.get("policy"), dict) and answers._provisional(result.get("policy")):
        # THE SAME CAVEAT the English answer gives: which captured detail the
        # unapplied rules wait on (attribute names as recorded)
        missing: list[str] = []
        for gap in result["policy"].get("unevaluated_rules") or []:
            for attribute in gap.get("missing_attributes") or []:
                if str(attribute).replace("_", " ") not in missing:
                    missing.append(str(attribute).replace("_", " "))
        provisional = languages.localized("checklist_provisional", language, attributes=", ".join(missing)) or ""
        if not provisional:
            return None

    def row(e: dict[str, Any]) -> str | None:
        status = str(e.get("status") or "").upper()
        word = languages.localized(f"status_{status}", language)
        if not word:
            return None
        said = f"{answers._readable(e.get('slot'))} — {word}"
        accepts = [str(a) for a in e.get("accepts") or []]
        if status in ("MISSING", "REJECTED", "REVIEW") and accepts and accepts != [str(e.get("slot"))]:
            names = [answers._readable(a) for a in accepts]
            listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + join + names[-1]
            said += languages.localized("checklist_any_one_of", language, items=listed) or ""
        if str(e.get("requirement") or "").upper() == "CONDITIONAL":
            said += languages.localized("checklist_conditional", language) or ""
        return said

    parts = []
    for fact, rows in (("checklist_required", [e for e in checklist if e.get("mandatory", True)]),
                       ("checklist_optional", [e for e in checklist if not e.get("mandatory", True)])):
        if not rows:
            continue
        said = [row(e) for e in rows]
        if not all(said):
            return None
        sentence = languages.localized(fact, language, count=str(len(rows)), items="; ".join(said))
        if not sentence:
            return None
        parts.append(sentence)
    if parts and provisional:
        parts.append(provisional)
    return " ".join(parts) or None


#: Answers whose localized form must keep every number the English one states
_FACT_CHECKED = frozenset({"KYC_RESULT", "DOCUMENT_VERIFICATION", "NEXT_ACTION", "DOCUMENTS_REQUIRED",
                           "DOCUMENT_DETAILS", "APPLICATION_STATUS"})


#: The document-details answers (facts/document_facts.py), values verbatim
_DETAILS_ALL = re.compile(r"^Details read from (?:your|the customer's|the) (.+?) \(it passed verification\):\n(.*)$", re.S)
_DETAILS_ONE = re.compile(r"^The (.+?) on (?:your|the customer's|the) (.+?) is (.+)\.$", re.S)


def _details_sentence(result: dict[str, Any], language: str) -> str | None:
    """What was read from a document: the heading in `language`, every field
    line exactly as the English answer listed it (names and values as read)."""
    from app.agents.applicant import language as languages

    english = str(result.get("answer") or "").strip()
    listed = _DETAILS_ALL.match(english)
    if listed:
        head = languages.localized("details_all", language, document=listed.group(1))
        return f"{head}\n{listed.group(2)}" if head else None
    one = _DETAILS_ONE.match(english)
    if one and "\n" not in english:
        return languages.localized("details_one", language, field=one.group(1), document=one.group(2),
                                   value=one.group(3))
    return None


#: The status answer's plain shapes (status_facts.answer): the stage, and the
#: recorded hold with its reason. Any further sentence keeps the English.
_STATUS = re.compile(r"^(?:Your|The) application is currently (?:under|at) (?:the )?(?P<stage>[^.]+?)(?: stage)?"
                     r"(?: and (?P<held>is under review|was declined)(?: because (?P<reason>.+?))?)?\.$", re.S)


def _status_sentence(result: dict[str, Any], language: str) -> str | None:
    """The case's status: its stage in `language`, the hold's reason quoted."""
    from app.agents.applicant import language as languages

    shape = _STATUS.match(str(result.get("answer") or "").strip())
    if not shape:
        return None
    said = languages.localized("status_now", language, stage=shape.group("stage"))
    if not said:
        return None
    if shape.group("held"):
        declined = shape.group("held") == "was declined"
        if shape.group("reason"):
            hold = languages.localized("status_declined" if declined else "status_review", language,
                                       reason=shape.group("reason"))
        else:
            hold = languages.localized("status_declined_plain" if declined else "status_review_plain", language)
        if not hold:
            return None
        said = f"{said} {hold}"
    return said


def _keeps_every_number(english: str, localized: str) -> bool:
    """Every number the English answer states is in the localized one too."""
    import re

    def numbers(text: str) -> set[str]:
        return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}

    return numbers(english) <= numbers(localized)


def languages_fixed(english: str, language: str) -> str | None:
    """The configured translation of a fixed English sentence (languages.yaml
    `fixed_sentences`), or None."""
    from app.agents.applicant import language as languages

    if not english or not language or language == "en" or not languages.enabled():
        return None
    table = languages._load().get("fixed_sentences") or {}
    entry = table.get(english)
    return str(entry.get(language)) if isinstance(entry, dict) and entry.get(language) else None


def apply(result: dict[str, Any], message: str) -> dict[str, Any]:
    """Localize the answer when a template covers it; always settle the
    contract's `reply_language` / `localized` to what the prose really is."""
    contract = result.get("language_contract")
    if not isinstance(contract, dict):
        return result
    wanted = str(contract.get("response_language") or "en")
    presented = result.get("_presented_language")
    sentence = None
    if wanted != "en" and presented != wanted and not result.get("guardrail") \
            and not result.get("clarification_required") and isinstance(result.get("answer"), str):
        intent = str(result.get("intent") or "")
        # A WHOLE ANSWER THAT IS ONE FIXED SENTENCE ("There is no co-applicant
        # on this application.") has its configured translation
        fixed = languages_fixed(str(result.get("answer") or "").strip(), wanted)
        if fixed:
            sentence = fixed
        elif intent == "APPLICATION_STAGE":
            sentence = _current_stage_sentence(result, message, wanted)
        elif intent == "DOCUMENTS_PENDING" and str(result.get("category") or "CASE_ONLY") == "CASE_ONLY":
            sentence = _pending_sentence(result, wanted)
        elif intent == "DOCUMENT_VERIFICATION":
            sentence = _verification_sentence(result, wanted)
        elif intent == "NEXT_ACTION":
            sentence = _next_sentence(result, wanted)
        elif intent == "DOCUMENTS_REQUIRED":
            sentence = _checklist_sentence(result, wanted)
        elif intent == "DOCUMENT_DETAILS":
            sentence = _details_sentence(result, wanted)
        elif intent == "APPLICATION_STATUS":
            sentence = _status_sentence(result, wanted)
        elif isinstance(result.get("gate"), dict):
            sentence = _gate_sentence(result, wanted)
        elif intent == "READINESS":
            sentence = _readiness_sentence(result, wanted)
        elif intent == "KYC_RESULT":
            sentence = _kyc_sentence(result, wanted)
        elif isinstance(result.get("pending_work"), dict):
            sentence = _work_sentence(result, wanted)
    if sentence and str(result.get("intent") or "") in _FACT_CHECKED             and not _keeps_every_number(result["answer"], sentence):
        # A TEMPLATE THAT DROPS A FACT DOES NOT REPLACE THE ANSWER: "unka KYC
        # score kya hai?" was answered with the score; a status-only sentence
        # in Hinglish would lose it. The English answer stands, and says so.
        sentence = None
    if sentence:
        from app.agents.applicant.copilot.answering import voice

        result["answer_en"] = result["answer"]
        # the SAME audience the English answer was written for (voice.py)
        result["answer"] = voice.for_audience(sentence)
        result["_presented_language"] = wanted
    return settle(result)


def settle(result: dict[str, Any]) -> dict[str, Any]:
    """The contract's `reply_language` / `localized`: the language the prose
    is REALLY in (a conversation reply already worded in it, or English)."""
    contract = result.get("language_contract")
    if isinstance(contract, dict):
        wanted = str(contract.get("response_language") or "en")
        reply = wanted if (wanted == "en" or result.get("_presented_language") == wanted) else "en"
        contract["reply_language"] = reply
        contract["localized"] = reply == wanted
    return result


__all__ = ["apply", "settle"]
