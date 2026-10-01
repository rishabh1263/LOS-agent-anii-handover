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
    return " ".join(sentences)


def _keeps_every_number(english: str, localized: str) -> bool:
    """Every number the English answer states is in the localized one too."""
    import re

    def numbers(text: str) -> set[str]:
        return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}

    return numbers(english) <= numbers(localized)


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
        if intent == "APPLICATION_STAGE":
            sentence = _current_stage_sentence(result, message, wanted)
        elif intent == "DOCUMENTS_PENDING" and str(result.get("category") or "CASE_ONLY") == "CASE_ONLY":
            sentence = _pending_sentence(result, wanted)
        elif isinstance(result.get("gate"), dict):
            sentence = _gate_sentence(result, wanted)
        elif intent == "READINESS":
            sentence = _readiness_sentence(result, wanted)
        elif intent == "KYC_RESULT":
            sentence = _kyc_sentence(result, wanted)
        elif isinstance(result.get("pending_work"), dict):
            sentence = _work_sentence(result, wanted)
    if sentence and str(result.get("intent") or "") == "KYC_RESULT"             and not _keeps_every_number(result["answer"], sentence):
        # A TEMPLATE THAT DROPS A FACT DOES NOT REPLACE THE ANSWER: "unka KYC
        # score kya hai?" was answered with the score; a status-only sentence
        # in Hinglish would lose it. The English answer stands, and says so.
        sentence = None
    if sentence:
        result["answer_en"] = result["answer"]
        result["answer"] = sentence
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
