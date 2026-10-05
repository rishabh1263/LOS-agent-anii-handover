"""
What this case was found to be, read back.

THE QUESTION THIS ANSWERS. "Why is this case in review?" The live store
says the case IS in review; it does not say why. The reasons were
computed by the LOS pipeline, returned in one response and -- until
Phase 1B -- lost. They are now in `case_findings`, `case_decisions` and
`case_events`, and this reads them.

IT IS NOT AUTHORISATION. Every function here takes a case_id and filters
on it. Whether the caller may see that case was decided before any of
this ran -- require_jwt, Caller, capability, then
`permissions.check_ownership`. Putting a second answer to that question
here would be two places to get it wrong, and the weaker one would win
by accident.

IT INVENTS NOTHING. Every reason in the summary is a reason code the
pipeline recorded. When nothing was recorded, the answer says so rather
than reaching for a plausible explanation -- an ungrounded "probably
because the PAN failed" is the worst thing this layer could produce,
because it is indistinguishable from a real one.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How many findings a summary names before it stops. A reviewer reading
#: a chat panel acts on the first few; twenty is a report, not an answer.
_MAX_REASONS = 6

#: Finding kinds whose reason codes explain a verdict. EXTRACTION is
#: excluded deliberately: it records what was read, not what was wrong.
_EXPLAINING_KINDS = ("VERIFICATION", "KYC", "FINANCIAL", "RISK", "RCU")

#: Reason code -> how it reads in a sentence. A code with no entry is
#: rendered from the code itself rather than dropped: an unnamed reason
#: is still a reason the reviewer needs to see.
_READABLE = {
    "DOCUMENT_TYPE_MISMATCH": "a document was not the type it was declared as",
    "NAME_MISMATCH": "the name differs across documents",
    "DOB_MISMATCH": "the date of birth differs across documents",
    "PAN_MISMATCH": "the PAN differs across documents",
    "FATHER_NAME_MISMATCH": "the father's name differs across documents",
    "ADDRESS_MISMATCH": "the address differs across documents",
    # What it means for the officer first, then the recorded fact behind
    # it -- the status answer says the same "more documents are needed".
    "INSUFFICIENT_SOURCES": ("more documents are needed for verification: "
                             "there was only one document to compare"),
    "INCOME_SINGLE_SOURCE": "income is evidenced by only one document",
    "VERIFICATION_INCONCLUSIVE": "verification could not reach a conclusion",
    "PROFILE_MISMATCH": "a declared detail did not match the documents",
    # The two a scanned statement produces. Without them the fallback
    # printed "document requires ocr and bank statement reconciliation
    # inconclusive" -- enum names in lower case, which is not English.
    "DOCUMENT_QUEUED_FOR_PROCESSING":
        "a long statement is still being read in the background",
    "DOCUMENT_REQUIRES_OCR":
        "a scanned document could not be read automatically",
    "BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE":
        "the bank statement's transactions could not be confirmed against "
        "its balance",
    "BANK_STATEMENT_RECONCILIATION_FAILED":
        "the bank statement's transactions did not add up to its balance",
    # Income consistency. Each says what was compared and what came of
    # it, and none of them says fraud: a salary slip and a statement
    # disagree for ordinary reasons, which is why the verdict is REVIEW.
    "INCOME_CONSISTENT":
        "the salary slip and the bank statement agree about income",
    "INCOME_AMOUNT_MISMATCH":
        "the salary stated on the slip differs from the credits the bank "
        "statement shows",
    "INCOME_INSUFFICIENT_HISTORY":
        "there was too little bank statement to compare income against",
    "SALARY_CREDIT_NOT_IDENTIFIED":
        "the recurring credits are not labelled as salary by the bank",
    "SALARY_SLIP_MISSING": "no salary slip was uploaded",
    "BANK_INCOME_EVIDENCE_MISSING":
        "the bank statement showed no recurring credit evidence",
    "INCOME_NOT_COMPARABLE":
        "the income figures could not be compared",
}


def _readable(code: str) -> str:
    known = _READABLE.get(code)
    if known:
        return known
    return str(code).replace("_", " ").lower()


def available() -> bool:
    """
    Whether case memory is switched on.

    Off by default. When off this module reports nothing rather than
    guessing from live state, because "we did not record it" and "there
    was nothing to record" are different answers.
    """
    from app.agents.los import config as los_config

    return los_config.case_memory_enabled()


def _repository():
    from app.store import get_repository

    return get_repository()


def case_memory(case_id: str, party_id: str | None = None) -> dict[str, Any]:
    """
    Findings, decisions and timeline for one case.

    CASE-SCOPED, AND PARTY-SCOPED WHEN ASKED. `party_id` narrows to that
    party plus the case-level findings that belong to nobody in
    particular -- a cross-document check is the case's, and hiding it
    because a party was named would lose the very thing that explains a
    two-party review.

    Never raises: a store that cannot be reached means no history, and
    the caller already handles that by saying so.
    """
    if not case_id or not available():
        return _empty()

    try:
        repository = _repository()
        # THE CURRENT FINDINGS, NOT THE HISTORY. Every reader below takes
        # "the" mismatch, "the" eligibility verdict, "the" income result
        # from this list; handed every run's rows, the first one it met
        # was the OLDEST -- a reprocessed case whose PAN name had been
        # corrected was still explained with the superseded name.
        from app.store import request_cache

        findings = request_cache.read(repository, "get_current_findings", case_id, party_id)             if party_id is not None else request_cache.read(repository, "get_current_findings", case_id)
        decisions = request_cache.read(repository, "get_case_decisions", case_id)
        timeline = request_cache.read(repository, "get_case_timeline", case_id)
    except Exception as exc:
        logger.warning("Case memory unavailable for %s: %r", case_id, exc)
        return _empty()

    # QUERIES AND DEVIATIONS ARE NOT VERDICTS: they are read as their own
    # block (app/agents/los/queries.py), never explained as a finding
    findings = [f for f in findings
                if str(getattr(f.finding_kind, "value", f.finding_kind)) not in ("QUERY", "DEVIATION")]
    return {
        "findings": [_public_finding(f) for f in findings],
        "decisions": [_public_decision(d) for d in decisions],
        "timeline": [_public_event(e) for e in timeline],
    }


def _empty() -> dict[str, Any]:
    return {"findings": [], "decisions": [], "timeline": []}


def _public_finding(finding: Any) -> dict[str, Any]:
    """
    One finding, as a caller sees it.

    AN ALLOWLIST. `payload` is deliberately absent: it holds the
    structured detail the pipeline published, and a chat answer needs
    the verdict and the reason, not the values. Publishing it here would
    put extracted identity data into a conversational response that had
    no reason to carry it.

    ONE NARROW EXCEPTION, `comparisons`. A cross-document mismatch is
    the case where the values ARE the reason: "the name differs across
    documents" leaves the reviewer with the one question they opened
    the case to answer -- differs HOW, and between whom. Only failed
    comparisons appear, only the field that was compared, and only the
    document type and value each side contributed. A comparison that
    passed publishes nothing, and no other part of the payload is
    published at all.
    """
    kind = finding.finding_kind
    row: dict[str, Any] = {
        "finding_kind": getattr(kind, "value", str(kind)),
        "status": finding.status,
    }
    for name in ("party_id", "source_id", "document_id", "score",
                 "confidence"):
        value = getattr(finding, name, None)
        if value is not None:
            row[name] = value
    if finding.reason_codes:
        row["reason_codes"] = list(finding.reason_codes)
    # WHICH DOCUMENT a document-level finding is about, by its own recorded
    # type -- so a follow-up's "the document" can name it.
    document_type = (getattr(finding, "payload", None) or {}).get("type")
    if document_type and row["finding_kind"] in {"VERIFICATION", "EXTRACTION"}:
        row["document_type"] = str(document_type).upper()

    comparisons = _failed_comparisons(getattr(finding, "payload", None))
    if comparisons:
        row["comparisons"] = comparisons
    # WHICH FIELDS a KYC check compared, and whether each matched -- names
    # and outcomes only; a value is published only for a failed comparison.
    checked = _checked_fields(getattr(finding, "payload", None))
    if checked and row["finding_kind"] == "KYC":
        row["checked"] = checked

    income = _income(finding)
    if income:
        row["income"] = income

    eligibility = _eligibility(finding)
    if eligibility:
        row["eligibility"] = eligibility
    return row


def _eligibility(finding: Any) -> dict[str, Any]:
    """
    The affordability verdict, where this finding is one.

    TOLD APART FROM INCOME BY `source_type`, not by inspecting the
    payload: both are FINANCIAL findings and both carry a `status`, so a
    shape-based guess would eventually hand an income reader an
    eligibility result and phrase it as a salary.

    PUBLISHED BECAUSE IT IS THE ANSWER to "am I eligible" and "what is
    my FOIR". Figures and a verdict; no transaction rows, no identity
    values, no document content.
    """
    if str(getattr(finding, "source_type", "") or "") != "ELIGIBILITY":
        return {}

    payload = getattr(finding, "payload", None)
    if not isinstance(payload, dict) or "status" not in payload:
        return {}

    # THE WHOLE RECORDED VERDICT. A read of what the pipeline published
    # must BE what it published: the LOS response, GET
    # /api/v1/eligibility/{case_id} and the Copilot all serve this, and a
    # subset here made the read endpoint differ from the response it was
    # reading. `evidence` is figures and their provenance -- no identity
    # data, no document content.
    keep = ("state", "eligible", "status", "reason_codes", "foir", "ltv", "inputs", "policy",
            "rules", "passed_rules", "failed_rules", "unevaluated_rules", "missing_information",
            "blockers", "configuration_gaps", "next_actions")
    return {k: payload[k] for k in keep if payload.get(k) is not None}


def _income(finding: Any) -> dict[str, Any]:
    """
    The income comparison, where this finding is one.

    PUBLISHED BECAUSE IT IS THE ANSWER. "Is my salary verified?" cannot
    be answered from a status word: the honest answer names what the
    slip states, what the statement evidences, and which of those two
    things exists. None of it is identity data -- amounts, months and a
    verdict -- and the transaction rows behind it stay where they are.
    """
    kind = getattr(finding, "finding_kind", None)
    if getattr(kind, "value", str(kind)) != "FINANCIAL":
        return {}

    # TOLD APART FROM THE ELIGIBILITY FINDING BY SOURCE. Both are
    # FINANCIAL and both carry a status; without this an affordability
    # verdict would be read back as an income comparison.
    source = str(getattr(finding, "source_type", "") or "")
    if source and source != "INCOME_CONSISTENCY":
        return {}

    payload = getattr(finding, "payload", None)
    if not isinstance(payload, dict) or "status" not in payload:
        return {}

    keep = ("status", "reason_codes", "bank_statement", "salary_slip",
            "difference", "tolerance")
    return {k: payload[k] for k in keep if payload.get(k) is not None}


def _checked_fields(payload: Any) -> list[dict[str, str]]:
    """The fields a KYC check compared and each one's outcome -- no values."""
    if not isinstance(payload, dict):
        return []
    return [{"field": str(f.get("field")), "status": str(f.get("status") or "").upper()}
            for f in payload.get("fields") or []
            if isinstance(f, dict) and f.get("field")]


def _failed_comparisons(payload: Any) -> list[dict[str, Any]]:
    """The compared fields that FAILED, and what each document said."""
    if not isinstance(payload, dict):
        return []

    out: list[dict[str, Any]] = []
    for field in payload.get("fields") or []:
        if not isinstance(field, dict):
            continue
        if str(field.get("status") or "").upper() != "FAIL":
            continue

        sources = [
            {"document_type": s.get("document_type"), "value": s.get("value")}
            for s in (field.get("sources") or [])
            if isinstance(s, dict) and s.get("value")
        ]
        if sources:
            out.append({"field": field.get("field"), "sources": sources})

    return out


def _public_decision(decision: Any) -> dict[str, Any]:
    row = {"decision": decision.decision, "next_action": decision.next_action,
           "status": decision.status}
    if decision.reason_codes:
        row["reason_codes"] = list(decision.reason_codes)
    for name in ("policy_id", "policy_version"):
        value = getattr(decision, name, None)
        if value:
            row[name] = value
    # WHEN it was decided, so a decision made at an earlier stage is never
    # reported as the current stage's.
    recorded = getattr(decision, "created_at", None)
    if recorded is not None:
        row["recorded_at"] = (recorded.isoformat()
                              if hasattr(recorded, "isoformat") else str(recorded))
    return {k: v for k, v in row.items() if v is not None}


def _public_event(event: Any) -> dict[str, Any]:
    row = {"event_type": event.event_type, "sequence": event.sequence}
    for name in ("stage", "summary", "party_id"):
        value = getattr(event, name, None)
        if value:
            row[name] = value
    return row


# ==========================================================================
# THE ANSWER
# ==========================================================================


#: The comparisons worth naming the values for. A mismatch here is
#: about identity, and "whose document is this" is answered by the two
#: values and by nothing else.
_NAMED_COMPARISONS = {
    "NAME": "name",
    "DATE_OF_BIRTH": "date of birth",
    "PAN_NUMBER": "PAN",
    "FATHER_NAME": "father's name",
}


def _mismatch_detail(findings: list[dict[str, Any]]) -> str:
    """
    The failed comparison, naming what each document said.

    WHY THIS IS PREFERRED OVER THE CODE'S SENTENCE. "The name differs
    across documents" is a description of a reason code. "The name on
    the PAN, X, does not match the name on the bank statement, Y" is
    the finding itself, and it is what the officer has to act on --
    they cannot decide whether two documents describe one person
    without seeing both names.

    IT IS STILL ENTIRELY RECORDED. Every value comes from the sources
    the pipeline wrote down at the time; nothing is looked up, joined
    or inferred here. When the values were not recorded, the caller
    falls back to the code's own sentence.
    """
    for finding in findings:
        for field in finding.get("comparisons") or []:
            label = _NAMED_COMPARISONS.get(
                str(field.get("field") or "").upper())
            sources = [s for s in (field.get("sources") or []) if s.get("value")]
            if not label or len(sources) < 2:
                continue

            first, second = sources[0], sources[1]
            return (
                f"the {label} on the "
                f"{_document_words(first.get('document_type'))}, "
                f"{first.get('value')}, does not match the "
                f"{_document_words(second.get('document_type'))} "
                f"{label}, {second.get('value')}"
            )

    return ""


def _document_words(document_type: Any) -> str:
    """A document type as a person says it."""
    words = {
        "PAN": "PAN",
        "BANK_STATEMENT": "bank account holder",
        "AADHAAR": "Aadhaar",
        "DRIVING_LICENCE": "driving licence",
        "VOTER_ID": "voter ID",
        "PASSPORT": "passport",
        "SALE_DEED": "sale deed",
    }
    key = str(document_type or "").upper()
    return words.get(key, key.replace("_", " ").lower() or "document")


def review_reason(
        memory: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """
    WHY this case stands where it does, as a clause, and what says so.

    A clause rather than a sentence because both callers put it after
    their own words -- "under review because ..." and "recorded as
    PARTIAL with a REVIEW decision. ..." -- and one of them has to
    capitalise it.

    THREE PLACES ARE READ, IN ORDER OF HOW MUCH THEY SAY, and all three
    are recorded:

      the compared values, when a failed comparison kept them
      the reason codes on the findings
      the reason codes on the decision itself

    THE LAST ONE MATTERS MORE THAN IT LOOKS. A case whose KYC block
    arrived at the top level recorded its verdict and no finding, and
    an explanation that reads only findings told the reviewer nothing
    was recorded -- beside a decision that said NAME_MISMATCH.

    Empty when nothing was recorded anywhere, which is the caller's
    signal to say so rather than to reach for an explanation.
    """
    findings = memory.get("findings") or []
    decisions = memory.get("decisions") or []

    # A FINDING THAT PASSED IS NOT A REASON.
    #
    # Income consistency records INCOME_CONSISTENT on a PASS, which is
    # worth keeping and is not an explanation of anything. Listing it
    # beside NAME_MISMATCH under "why is this case in review" would
    # offer a reviewer a reason that is not one.
    explaining = [
        f for f in findings
        if f.get("reason_codes")
        and f.get("finding_kind") in _EXPLAINING_KINDS
        and str(f.get("status") or "").upper() not in {"PASS", "SKIPPED"}
    ]

    concrete = _mismatch_detail(findings)
    if concrete:
        return concrete, _sources(findings=explaining or findings,
                                  decisions=decisions)

    from app.agents.verification.reasons import POSITIVE_CODES

    codes: list[str] = []
    for finding in explaining:
        for code in finding.get("reason_codes") or []:
            if str(code).upper() in POSITIVE_CODES:
                continue                    # found / confirmed: not a reason
            if code not in codes:
                codes.append(code)

    if not codes and decisions:
        for code in decisions[-1].get("reason_codes") or []:
            if code not in codes:
                codes.append(code)

    if not codes:
        return "", []

    shown = codes[:_MAX_REASONS]
    more = len(codes) - len(shown)
    tail = f", and {more} further finding(s)" if more > 0 else ""

    return (_and_list([_readable(code) for code in shown]) + tail,
            _sources(findings=explaining, decisions=decisions))


def explain(memory: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """
    Why the case stands where it does, and what says so.

    Returns the sentence and the sources behind it. EVERY CLAUSE COMES
    FROM A RECORDED REASON CODE -- there is no model here and no
    inference. When nothing was recorded the sentence says exactly that,
    because a confident explanation assembled from nothing is the one
    output a reviewer cannot check.
    """
    findings = memory.get("findings") or []
    decisions = memory.get("decisions") or []

    if not findings and not decisions:
        return (
            "No findings have been recorded for this case yet, so there is "
            "nothing on file explaining its current state.",
            [],
        )

    latest = decisions[-1] if decisions else {}
    clause, sources = review_reason(memory)

    # A HOLD IS SAID AS ONE SENTENCE WITH ITS REASON: "Your application is
    # under review because the name on the PAN, X, does not match ...".
    # The internal words -- PARTIAL, a REVIEW "decision" -- are how the
    # pipeline records it, not how a person is told it.
    held = _HELD.get(str(latest.get("decision") or "").upper())
    if held:
        if not clause:
            return (f"Your application {held}. No individual findings "
                    f"were recorded against it.",
                    _sources(decisions=decisions))
        return f"Your application {held} because {clause}.", sources

    head = _verdict_sentence(latest)
    if not clause:
        return (
            f"{head} No individual findings were recorded against it.",
            _sources(decisions=decisions),
        )

    return f"{head} {clause[0].upper()}{clause[1:]}.", sources


def findings_summary(memory: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """
    The findings recorded on the case, listed as recorded: kind, status and
    reason codes. Nothing is explained or inferred; a case with no findings
    is told exactly that.
    """
    findings = memory.get("findings") or []
    if not findings:
        return ("No findings or reason codes have been recorded for this case yet.", [])
    lines = []
    for row in findings:
        kind = str(row.get("finding_kind") or "").replace("_", " ").title() or "Finding"
        status = str(row.get("status") or "recorded").lower()
        codes = row.get("reason_codes") or []
        said = f"{kind} -- {status}"
        if row.get("document_type"):
            said += f" ({str(row['document_type']).replace('_', ' ').title()})"
        if codes:
            said += ": " + ", ".join(str(c).replace("_", " ").lower() for c in codes)
        lines.append(said)
    noun = "finding" if len(findings) == 1 else "findings"
    return (f"{len(findings)} {noun} recorded on this case: " + "; ".join(lines) + ".",
            _sources(findings=findings))


#: A recorded KYC status, as it is said of a check.
_KYC_STATE = {"PASS": "passed", "SUCCESS": "passed", "VERIFIED": "passed",
              "REVIEW": "needs review", "FAIL": "did not pass", "REJECTED": "did not pass",
              "PENDING": "is still pending"}


def _kyc_doc(document_type: Any) -> str:
    raw = str(document_type or "a document").upper()
    return raw if raw in ("PAN", "KYC") else raw.replace("_", " ").lower()


def _kyc_mismatch(comparisons: list[dict[str, Any]]) -> str:
    """ "the name didn't match -- the PAN says X, but the bank statement says Y" """
    from app.security import sensitivity

    parts = []
    for c in comparisons:
        field = str(c.get("field") or "a field").replace("_", " ").lower()
        said = [f"the {_kyc_doc(s.get('document_type'))} says "
                f"{sensitivity.mask_identifiers(str(s.get('value')))}"
                for s in c.get("sources") or []]
        detail = (": " + ", but ".join(said)) if said else ""
        parts.append(f"the {field} didn't match{detail}")
    return "; ".join(parts)


def kyc_answer(memory: dict[str, Any], *, want_score: bool = False, want: str | None = None,
               party_id: str | None = None, primary_id: str | None = None,
               who: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """
    THE RECORDED KYC RESULT, and only that. Three honest outcomes: a KYC
    finding with a score; a KYC finding without one; no KYC finding at all.
    The downstream KYC decision is not this, and is not claimed.

    `party_id` narrows it to ONE PERSON's check (a finding with no party is
    the primary applicant's); `who` names that person in the sentence
    ("the co-applicant"), None for the caller's own. `want` is what was
    asked: result, score, mismatch (why / which fields failed) or fields.
    """
    want = want or ("score" if want_score else "result")
    kyc = [row for row in (memory.get("findings") or [])
           if str(row.get("finding_kind") or "").upper() == "KYC"]
    if party_id is not None:
        kyc = [row for row in kyc
               if str(row.get("party_id") or "") == party_id
               or (not row.get("party_id") and party_id == primary_id)]
    try:
        from app.agents.applicant.copilot.facts import field_state as _field_state

        latest_row = kyc[-1] if kyc else {}
        if not kyc:
            _state = "NOT_AVAILABLE"
        elif want == "score":
            _state = "PRESENT" if latest_row.get("score") not in (None, "") else "NOT_AVAILABLE"
        else:
            _state = "PRESENT"
        _field_state.record(f"kyc_{want}", _state, "case_findings:KYC",
                            party=("CO_APPLICANT" if who else None))
    except Exception:  # noqa: BLE001 - evidence is a record of the answer, never its cause
        pass
    try:
        from app.agents.applicant.copilot.answering import structured as _structured

        _role = "CO_APPLICANT" if who and "co-applicant" in str(who) else "PRIMARY_APPLICANT"
        _structured.put("kyc", _role, _structured.kyc_block(kyc[-1] if kyc else None, party_role=_role))
    except Exception:  # noqa: BLE001 - the structured block never costs the answer
        pass
    if not kyc:
        if who:
            return (f"No KYC result has been recorded for {who} yet, so I can't tell you "
                    f"their KYC status.", [])
        return ("KYC information is not yet available for this case: no KYC check has been "
                "recorded.", [])
    latest = kyc[-1]
    whose = f"{who}'s" if who else "your"
    Whose = whose[0].upper() + whose[1:]
    raw_status = str(latest.get("status") or "").upper()
    state = _KYC_STATE.get(raw_status, f"is recorded as {raw_status.lower() or 'recorded'}")
    score = latest.get("score")
    codes = [str(c).replace("_", " ").lower() for c in latest.get("reason_codes") or []]
    comparisons = latest.get("comparisons") or []
    why = _kyc_mismatch(comparisons) if comparisons else (
        f"of {_and_list(codes)}" if codes else "")
    if raw_status in ("SKIPPED", "NOT_RUN", "NOT_EVALUATED"):
        score = None                 # a check that did not run scored nothing -- never "0"
    elif str(score) in ("0", "0.0") and not comparisons and not [
            f for f in (latest.get("checked") or [])
            if isinstance(f, dict) and str(f.get("status") or "").upper() not in ("SKIPPED", "NOT_RUN", "MISSING")]:
        score = None                 # nothing was compared ("insufficient sources"): a 0 is not a measurement
    because = f" because {why}" if why and raw_status not in ("PASS", "SUCCESS", "VERIFIED") else ""
    sources = _sources(findings=kyc)
    if want == "score":
        if score in (None, ""):
            return (f"{Whose} KYC check is recorded -- it {state} -- but no KYC score was "
                    f"recorded with it.", sources)
        return f"{Whose} recorded KYC score is {score}; the check {state}.", sources
    if want == "fields":
        checked = latest.get("checked") or [{"field": c.get("field"), "status": "FAIL"}
                                            for c in comparisons]
        if not checked:
            return (f"{Whose} KYC check is recorded (it {state}), but it doesn't list the "
                    f"individual fields it compared.", sources)
        listed = [f"{str(c.get('field')).replace('_', ' ').lower()} "
                  f"({'matched' if c.get('status') in ('PASS', 'MATCH') else 'did not match'})"
                  for c in checked]
        return f"{Whose} KYC check compared: {_and_list(listed)}.", sources
    if want == "mismatch":
        if comparisons:
            return f"{Whose} KYC check {state} because {_kyc_mismatch(comparisons)}.", sources
        if raw_status in ("PASS", "SUCCESS", "VERIFIED"):
            return f"Nothing failed in {whose} KYC check -- it passed.", sources
        if codes:
            return f"{Whose} KYC check {state} because of {_and_list(codes)}.", sources
        return (f"{Whose} KYC check {state}, but no field-level reason was recorded with it.",
                sources)
    said = f"{Whose} KYC check {state}{because}."
    if score not in (None, ""):
        said += f" The recorded score is {score}."
    return said, sources


#: A recorded decision that holds the application, as it is said.
_HELD = {"REVIEW": "is under review", "REJECT": "was declined"}

#: A recorded decision or processing status, in words.
_DECISION_WORDS = {"PASS": "passed its checks", "FAIL": "did not pass its checks"}
_STATUS_WORDS = {"SUCCESS": "fully processed", "PARTIAL": "partly processed",
                 "FAILED": "not processed"}


def _verdict_sentence(verdict: dict[str, Any]) -> str:
    decision = str(verdict.get("decision") or "").upper()
    status = str(verdict.get("status") or "").upper()

    if decision in _DECISION_WORDS:
        return f"Your application {_DECISION_WORDS[decision]}."
    if decision:
        return f"Your application is recorded as {decision.lower()}."
    if status:
        return (f"Your application was "
                f"{_STATUS_WORDS.get(status, status.lower())}.")
    return "Your application has recorded findings."


def _sources(
    findings: list[dict[str, Any]] | None = None,
    decisions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    What the answer rests on.

    One entry per reason, naming the finding that carried it -- so a
    reviewer can go to the document rather than take the sentence on
    trust. Never a payload, never a raw row.
    """
    sources: list[dict[str, Any]] = []

    for finding in (findings or [])[:_MAX_REASONS]:
        for code in (finding.get("reason_codes") or [])[:_MAX_REASONS]:
            entry = {
                "kind": "case_finding",
                "finding_kind": finding.get("finding_kind"),
                "reason_code": code,
            }
            for name in ("document_id", "source_id", "party_id"):
                if finding.get(name):
                    entry[name] = finding[name]
            sources.append(entry)

    for decision in (decisions or [])[-1:]:
        sources.append({
            "kind": "case_decision",
            "decision": decision.get("decision"),
            "status": decision.get("status"),
        })

    return sources


def _and_list(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


__all__ = ["available", "case_memory", "explain", "review_reason"]
