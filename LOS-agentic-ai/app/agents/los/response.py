"""
The public shape of POST /api/v1/los/process.

One place assembles the client-facing envelope, so what a client sees is
decided here rather than by whichever producer happened to fill a dict.

WHAT IS DELIBERATELY NOT EXPOSED: OCR tokens, per-candidate scores, the KYC
pair-comparison matrix, MCP envelopes, raw model output. Those are how the
answer was reached, not the answer, and a caller that starts parsing them
turns an internal detail into a contract nobody meant to sign.

WHAT IS EXPOSED IS DERIVED, NOT INVENTED. `conflicts` comes from the KYC
checks that actually failed -- KYC already computes name, date-of-birth,
address and PAN agreement across documents, and a conflict is simply one of
those saying no. `decision` is the deterministic status roll-up that already
governed the response. Neither is a new engine.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.verification import reasons as reasons_module

# Severity of a failed KYC check, by the check it came from. A mismatch on
# identity is a different matter from one on address, and a reviewer sorting
# a queue needs to see which is which.
_CONFLICT_SEVERITY = {
    "PAN": "HIGH",
    "NAME": "HIGH",
    "DATE_OF_BIRTH": "HIGH",
    "DOB": "HIGH",
    "INCOME": "MEDIUM",
    "ADDRESS": "MEDIUM",
}

_TERMINAL_STATUSES = ("FAILED", "REJECTED", "REVIEW", "PARTIAL", "SUCCESS")

#: No document in the application cleared verification, so there is no
#: verified evidence for a decision to rest on.
NO_VERIFIED_DOCUMENTS = "NO_VERIFIED_DOCUMENTS"

#: The uploaded document was not the type the caller said it would be.
#: Distinct from an unreadable document: the caller can fix this one by
#: sending the right file, so it gets its own next action.
DOCUMENT_TYPE_MISMATCH = "DOCUMENT_TYPE_MISMATCH"



def verification_status(document: dict[str, Any]) -> str:
    """
    One document's verification verdict, whichever producer wrote it.

    Two producers, two spellings: the Document Agent writes
    verification.status, a specialist writes verification.decision and
    repeats it on its own payload. Resolved HERE and nowhere else, because
    reading only `status` once made every specialist document report
    SKIPPED -- and the extraction gate keys on this value, so it silently
    withheld their fields too.
    """
    verification = document.get("verification") or {}
    specialist = document.get("specialist") or {}

    return str(
        verification.get("status")
        or verification.get("decision")
        or specialist.get("decision")
        or "SKIPPED"
    )


def any_document_verified(documents: list[dict[str, Any]]) -> bool:
    """Did anything actually clear verification?"""
    return any(
        verification_status(document).upper() == "PASS" for document in documents
    )


def conflicts_from_kyc(kyc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """
    The disagreements KYC found between documents.

    Derived from the per-check results KYC already produces. A check that
    FAILED is a conflict; one that was SKIPPED is not -- absent evidence is
    not disagreement, and reporting it as one would fill the queue with
    documents nobody actually needs to look at.
    """
    if not kyc:
        return []

    conflicts: list[dict[str, Any]] = []

    for check in kyc.get("checks") or []:
        status = str(check.get("status") or "").upper()
        if status not in {"FAIL", "REVIEW"}:
            continue

        name = str(check.get("check") or check.get("name") or "").upper()
        conflicts.append(
            {
                "type": f"{name}_MISMATCH" if name else "MISMATCH",
                "severity": _CONFLICT_SEVERITY.get(name, "MEDIUM"),
                "status": status,
                "reason_codes": list(check.get("reason_codes") or []),
                "sources": list(check.get("source_ids") or []),
            }
        )

    return conflicts



#: Verdicts that owe the caller an explanation.
#:
#: SKIPPED is not one of them: a stage that did not run has no finding to
#: report, and inventing a reason for it would describe a check that never
#: happened. PASS is not one either -- a clean document annotated with
#: reason codes reads as a document with problems.
_NEEDS_A_REASON = {"REVIEW", "FAIL", "REJECTED", "FAILED"}


#: What a published KYC field row carries. An allowlist, not a filter: a new
#: internal key is absent from the response until somebody adds it here and
#: decides it belongs in front of a customer-facing operator.
_PUBLIC_FIELD_KEYS = (
    "field", "status", "match_score", "confidence", "reason_code", "reason",
    # `reason` is dropped below wherever `reason_code` is present; it
    # stays in the allowlist for the rare row that reaches a verdict
    # without one, where the sentence is the only explanation there is.
    # WHOSE ROW THIS IS, on a case-level list covering two parties: two
    # people produce two NAME rows and a reviewer must be able to tell
    # them apart. Set only when the case actually has a co-applicant, and
    # the allowlist drops absent keys -- so a single-applicant response
    # carries exactly the keys it carried before.
    "party_id",
)

#: What a published source carries. `source_id` is the caller's own filename,
#: echoed back so they can tie a row to the file they sent.
_PUBLIC_SOURCE_KEYS = ("source_id", "document_type", "value", "normalized_value")


# ==========================================================================
# JSON-NATIVE VALUES
# ==========================================================================

#: The longest a published extracted value may be before it is treated as
#: OCR spill rather than a field. A PAN is 10 characters, a name rarely
#: over 50, a printed address under 150. Past this, what is being
#: published is the recogniser's output, not a value.
_MAX_VALUE_CHARS = 160

#: The longest a free-text address component may be. A locality, street
#: or house number past this is the unplaced remainder of an OCR line,
#: not a component.
_MAX_COMPONENT_CHARS = 40

#: Nested keys that restate something the document already says.
#: `signals.source_document` is "BANK_STATEMENT" on a document whose
#: `type` is BANK_STATEMENT.
_REDUNDANT_KEYS = {"source_document"}

#: Any string carrying one of these is a Python repr that escaped, not a
#: value anybody meant to publish.
_REPR_MARKERS = ("Decimal(", "datetime.", "object at 0x", "=None ", "None>")


def jsonable(value: Any) -> Any:
    """
    One extracted value, in a form JSON can carry honestly.

    THE DEFECT THIS CLOSES. A Decimal reached the response as the string
    `"Decimal('55435.71')"`, and an income model as
    `"monthly_net_salary=None ... average_monthly_credit=Decimal('55435.71')"`.
    Both are Python reprs: a client cannot parse them, and they publish
    how this service is built. Numbers go out as numbers.
    """
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(value, Decimal):
        # float(), not str(): a client reading JSON wants a number.
        return float(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    # A model or object. Its repr is internal by definition.
    return None


def _numeric(value: Any) -> Any:
    """A decimal-looking string as a number, or the value unchanged."""
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text) if "." in text else int(text)
        except ValueError:
            return value
    return value


def _is_repr(value: Any) -> bool:
    return isinstance(value, str) and any(m in value for m in _REPR_MARKERS)


def _structured_address(raw: Any) -> dict[str, str] | None:
    """
    An address as components, or nothing.

    ONE SHAPER, EVERYWHERE. Delegates to `address_public.public_address`
    so the same address published on a document and inside a KYC source
    is the same object. It was not: `value` came back
    `{"state": "MAHARASHTRA", "house": "514"}` while `normalized_value`
    beside it carried a pincode too, because the two went through
    different paths.

    THAT SHAPER IS PUBLIC-ONLY. KYC still parses and compares exactly
    what it parsed before -- see app/agents/kyc/address.py. Nothing
    here reaches a score or a verdict.
    """
    from app.agents.los.address_public import public_address

    return public_address(raw)


def public_extraction_values(
    fields: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Extracted fields, in a form a client can actually consume.

    THREE THINGS HAPPEN HERE, and nothing else. No value is invented, no
    value is corrected, and no verdict is touched -- the gate upstream
    already decided what may be released at all.

      NUMBERS GO OUT AS NUMBERS. The financial signals arrived as
      Decimals and as decimal-looking strings; both are published as
      JSON numbers.

      AN ADDRESS GOES OUT AS COMPONENTS, or not at all.

      OCR SPILL IS DROPPED. A value longer than a field plausibly is, or
      carrying a Python repr, is the recogniser's output rather than a
      reading of a field, and an omitted field is more honest than a
      transcript presented as one.
    """
    released: dict[str, Any] = {}

    for name, value in (fields or {}).items():
        if name == "address":
            structured = _structured_address(value)
            if structured:
                released[name] = structured
            continue

        cleaned = jsonable(value)

        if isinstance(cleaned, dict):
            # `signals` and `evidence` on a bank statement: money held as
            # strings so no precision was lost in transit. A client
            # wants numbers.
            cleaned = {k: _numeric(v) for k, v in cleaned.items()
                       if k not in _REDUNDANT_KEYS}
            cleaned = {k: v for k, v in cleaned.items() if v is not None}
            if cleaned:
                released[name] = cleaned
            continue

        if cleaned is None or _is_repr(cleaned):
            continue
        if isinstance(cleaned, str) and len(cleaned) > _MAX_VALUE_CHARS:
            continue

        released[name] = cleaned

    return released


def public_kyc_fields(kyc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """
    The field-level KYC rows, as a caller sees them.

    Built through an allowlist so nothing internal can leak by being added
    upstream. In particular `confidence_factors` -- the breakdown of how the
    confidence was reached -- stays OFF the response by default: it is a
    debugging aid, it names policy internals, and a field officer acting on a
    case does not read it. It is available through the KYC agent's own
    configuration endpoint for anyone who needs to audit the model.
    """
    rows: list[dict[str, Any]] = []

    for field in (kyc or {}).get("fields") or []:
        row = {key: field.get(key) for key in _PUBLIC_FIELD_KEYS
               if field.get(key) is not None}

        # THE CODE ALREADY SAYS IT. `NAME_MISMATCH` beside "Name differs
        # across PAN and Driving Licence." is the same fact twice, and
        # the sentence was the single largest thing in a party's KYC
        # after the sources. Kept only where there is no code to read.
        if row.get("reason_code"):
            row.pop("reason", None)

        # AN ADDRESS SOURCE CARRIES THE SAME OCR LINE THE DOCUMENT DID.
        # Cleaned the same way, or this is a second door out for the
        # text the extraction shaping just closed.
        is_address = str(field.get("field") or "").upper() == "ADDRESS"

        sources = []
        for source in field.get("sources") or []:
            published = {}
            for key in _PUBLIC_SOURCE_KEYS:
                value = jsonable(source.get(key))
                if is_address and key in ("value", "normalized_value"):
                    # ONE ADDRESS OBJECT, NOT TWO SHAPES. `value` and
                    # `normalized_value` came from different paths and
                    # disagreed -- `{"state":..., "house":...}` beside
                    # `{"pincode":..., "state":..., "house":...}` for
                    # the same address. The canonical shaping IS the
                    # normalisation, so there is one object and it is
                    # published as `value`.
                    if key == "normalized_value":
                        continue
                    shaped = _structured_address(value)
                    if shaped:
                        published[key] = shaped
                    continue
                # A MODEL REPR IS NOT A VALUE. The INCOME row published
                # `monthly_net_salary=None ... Decimal('55435.71')` --
                # the income model printed. Omitted rather than tidied:
                # the number a reviewer wants is already on the
                # document's own extraction.
                if value is None or _is_repr(value):
                    continue
                if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
                    continue
                published[key] = value

            # NORMALISATION THAT CHANGED NOTHING IS NOT WORTH SAYING.
            # `normalized_value` is published beside `value` so a
            # near-miss can be read correctly -- the difference is
            # either in the documents or in the normalisation, and only
            # showing both says which. When they are identical there is
            # no difference to explain, and on most rows they were
            # identical.
            if published.get("normalized_value") == published.get("value"):
                published.pop("normalized_value", None)

            if published:
                sources.append(published)
        if sources:
            row["sources"] = sources

        rows.append(row)

    return rows


#: Order of severity for the cross-document roll-up.
_CHECK_RANK = {"PASS": 0, "SKIPPED": 0, "REVIEW": 1, "FAIL": 2}


def cross_document_from(kyc: dict[str, Any] | None) -> dict[str, Any]:
    """
    Agreement BETWEEN documents, as one object.

    Replaces the older `conflicts` list, which reported only disagreements
    and so could not distinguish "every field agreed" from "nothing was
    comparable" -- both arrived as []. A reviewer needs that difference:
    the first is evidence, the second is the absence of it.

    `checks` is the five named checks KYC already performs, not the
    pair-comparison matrix behind them. That matrix stays internal; it grows
    with the square of the document count and says nothing a reviewer acts on.

    THE ROLL-UP IS CAPPED BY POLICY, THE CHECKS ARE NOT. A check that failed
    still reports FAIL with its reason codes, the documents involved and what
    each of them said. Whether that FAIL is allowed to carry the overall
    status down to FAIL -- rather than REVIEW -- is a business decision, and
    it is read from kyc_policies.yaml rather than made here. Documents
    disagreeing is a reason to involve a human, not a finding of fraud.
    """
    checks: list[dict[str, Any]] = []
    worst = "SKIPPED"
    compared = False

    for check in (kyc or {}).get("checks") or []:
        status = str(check.get("status") or "SKIPPED").upper()

        entry: dict[str, Any] = {
            "check": check.get("check"),
            "status": status,
            "reason_codes": list(check.get("reason_codes") or []),
        }

        sources = list(check.get("source_ids") or [])
        if sources:
            entry["sources"] = sources

        # Attached only where something disagreed. On a passing check the
        # values are already in documents[].extraction and repeating them
        # would double the size of a clean response for no reader.
        if status in {"FAIL", "REVIEW"}:
            values = {
                str(source): str(value)
                for source, value in (check.get("values") or {}).items()
            }
            if values:
                entry["details"] = values

        checks.append(entry)

        if status == "SKIPPED":
            continue

        compared = True

        # A non-blocking check contributes at most REVIEW, whatever it found.
        effective = status
        if status == "FAIL" and not check.get("blocking", False):
            effective = "REVIEW"

        if _CHECK_RANK.get(effective, 1) > _CHECK_RANK.get(worst, 0):
            worst = effective

    # Nothing comparable is not agreement. Reported as SKIPPED rather than
    # PASS, for the same reason a decision cannot pass on nothing verified.
    status = worst if compared else "SKIPPED"
    if compared and worst == "SKIPPED":
        status = "PASS"

    return {"status": status, "checks": checks}


def decision_from(
    status: str,
    kyc: dict[str, Any] | None,
    conflicts: list[dict[str, Any]],
    documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    The deterministic outcome of this application, as computed.

    This is NOT a credit decision and not a policy engine: it restates the
    worst-wins roll-up the flow already produced, with the reasons that drove
    it, under a name a client can read. Nothing here weighs affordability or
    approves anything.

    One rule is enforced rather than restated: a PASS has to mean something
    was actually verified. With verification switched off every document
    reports SKIPPED and KYC has no sources, and both of those rank as
    harmless -- so the roll-up read SUCCESS and the decision read PASS for an
    application in which nothing had been checked. SKIPPED is not a pass
    anywhere else in this service and it is not one here.
    """
    reason_codes: list[str] = []

    if kyc:
        reason_codes.extend(str(code) for code in kyc.get("reason_codes") or [])

    for conflict in conflicts:
        reason_codes.extend(conflict.get("reason_codes") or [])

    mapped = {
        "SUCCESS": "PASS",
        "PARTIAL": "REVIEW",
        "REVIEW": "REVIEW",
        "REJECTED": "REJECT",
        "FAILED": "REJECT",
    }.get(str(status).upper(), "REVIEW")

    # A HIGH-severity disagreement between documents is never a pass, however
    # cleanly each document read on its own.
    if mapped == "PASS" and any(c["severity"] == "HIGH" for c in conflicts):
        mapped = "REVIEW"

    # Nothing verified is not a pass. Checked against the documents rather
    # than against the status, because the status is exactly what goes wrong
    # here: SKIPPED ranks alongside SUCCESS, so an application of entirely
    # unverified documents rolled up clean.
    if documents is not None and not any_document_verified(documents):
        reason_codes.append(NO_VERIFIED_DOCUMENTS)
        if mapped == "PASS":
            mapped = "REVIEW"

    return {
        "status": mapped,
        "reason_codes": sorted(set(reason_codes)),
        # Said plainly so nobody reads this as an underwriting outcome.
        "basis": "DETERMINISTIC_DOCUMENT_AND_KYC_RESULTS",
    }


def next_action_from(
    status: str,
    kyc: dict[str, Any] | None,
    conflicts: list[dict[str, Any]],
    documents: list[dict[str, Any]],
) -> str:
    """
    What should happen to this application next.

    A deterministic mapping over results that are already final -- not a
    recommendation engine, and nothing here weighs policy. It exists so a
    caller does not have to re-derive the obvious from four other fields.
    """
    # A wrong document and an unreadable one need different things from the
    # applicant: one has the document and sent the wrong file, the other
    # needs to re-photograph what they sent. Checked first, because a type
    # mismatch is also a verification FAIL.
    if any(
        DOCUMENT_TYPE_MISMATCH in (d.get("reason_codes") or [])
        for d in documents
    ):
        return "REQUEST_CORRECT_DOCUMENT"

    if any(d.get("verification") == "FAIL" for d in documents):
        return "REQUEST_VALID_DOCUMENT"

    if any(c.get("severity") == "HIGH" for c in conflicts):
        return "MANUAL_REVIEW"

    if any(d.get("verification") in {"REVIEW", "SKIPPED"} for d in documents):
        return "MANUAL_REVIEW"

    if str((kyc or {}).get("status", "")).upper() in {"FAIL", "REVIEW"}:
        return "MANUAL_REVIEW"

    if str(status).upper() in {"SUCCESS"}:
        return "CONTINUE"

    return "MANUAL_REVIEW"


def compact_document(document: dict[str, Any]) -> dict[str, Any]:
    """
    One document, in the mid-short public shape.

    Flat by design: a caller wants the type, whether it passed, and the
    fields. Verification is a WORD, not an object, because a status plus its
    reason codes is two keys where one usually does.

    Reason codes appear only when there are any, and extraction only when the
    gate released something -- an always-present empty object trains callers
    to ignore it.
    """
    verification = document.get("verification") or {}
    extraction = document.get("extraction") or {}
    specialist = document.get("specialist") or {}

    status = verification_status(document)

    compact: dict[str, Any] = {
        "source_id": document.get("source_id"),
        "type": (document.get("document") or {}).get("type") or "UNKNOWN",
        # The per-document roll-up. Kept alongside `verification` because the
        # two answer different questions: a document that could not be
        # processed at all reports FAILED here while its verification is
        # SKIPPED, which is otherwise indistinguishable from verification
        # having been switched off.
        "status": document.get("status"),
        "verification": status,
    }

    # WHOSE DOCUMENT THIS IS.
    #
    # Carried on every document so a frontend can split a case into its
    # two parties without guessing from filenames or upload order.
    # Present only when the flow stamped it, so a response from a caller
    # that never supplied parties is unchanged.
    for field_name in ("party_id", "party_role"):
        if document.get(field_name):
            compact[field_name] = document[field_name]

    # NOT PRESENT, DELIBERATELY: `category` and the per-stage timings.
    #
    # `category` duplicated the capability name on a specialist document and
    # was derivable from `type` on every other, so it published an internal
    # routing decision and told a client nothing. The timings
    # (classification_ms, ocr_ms, specialist_ms, mcp_ms, orchestration_ms)
    # are diagnostics: they describe how this service is built, and a client
    # that started reading them would turn that into a contract. They remain
    # on the internal envelope and in the orchestration logs.

    # THE VERIFICATION GATE. Extraction is released only behind a PASS.
    #
    # Shared with the detailed shape rather than re-implemented, because that
    # is exactly how it got lost once: the compact path read the fields
    # straight off the document and a specialist REVIEW handed back its
    # extraction anyway. A gate written twice is a gate enforced once.
    # CLEANED HERE, NOT IN THE GATE. `released_extraction` is shared with
    # profile matching and KYC, which compare the RAW normalised values;
    # structuring an address or dropping a long value for them would
    # change what gets matched. The cleaning belongs on the way out.
    released = released_extraction(extraction, status)
    if released and released.get("fields"):
        values = public_extraction_values(released["fields"])
        if values:
            compact["extraction"] = values

    # A stage that was switched off says so on the document it would have
    # handled. Without this, "extraction is absent" is indistinguishable from
    # "extraction found nothing", and an operator who disabled it cannot tell
    # their own change took effect.
    from app.agents.los import config as los_config

    withheld: list[str] = []
    if status.upper() == "PASS" and not los_config.extraction_enabled():
        withheld.append("EXTRACTION_DISABLED")

    codes = list(verification.get("reason_codes") or []) + withheld

    # A NON-PASS ALWAYS SAYS SOMETHING.
    #
    # Several paths produce a verdict and a code; at least one produced a
    # verdict and nothing. A REVIEW with an empty `reason_codes` is a dead
    # end for the officer holding the document and for the queue trying to
    # route it, and it is invisible in testing because the verdict looks
    # right. Closed HERE, at the one point every path passes through,
    # rather than trusting each of them to remember.
    if not codes and status.upper() in _NEEDS_A_REASON:
        codes = ["VERIFICATION_INCONCLUSIVE"]

    if codes:
        compact["reason_codes"] = codes

    # WHY, IN A SENTENCE. A reason code routes a queue; a person still has to
    # know what to do. A REVIEW that carries neither is a dead end.
    #
    # The verifier's own sentences win where it wrote any: it saw the
    # document and the catalogue did not. Where it wrote none, the
    # catalogue supplies one per code, so no non-pass reaches a caller as
    # a bare identifier. Sale deeds carried four codes and no prose at
    # all; identity documents carried prose only when the image was poor.
    sentences = reasons_module.explain_all(
        codes, existing=verification.get("reasons"),
    )
    if sentences and status.upper() in _NEEDS_A_REASON:
        compact["reasons"] = sentences
    elif verification.get("reasons"):
        # A PASS may still carry a note the verifier chose to write. It is
        # not withheld, but nothing is invented for it either.
        compact["reasons"] = list(verification["reasons"])

    # How much of what should have been established was, and how far that
    # answer can be relied on. NOT a risk or credit score -- they describe
    # the document and the checking of it, and no amount of money on a
    # statement moves either one.
    for field in ("verification_score", "verification_confidence"):
        if verification.get(field) is not None:
            compact[field] = verification[field]

    # Said plainly rather than inferred from whether `extraction` is present.
    compact["has_extracted_fields"] = bool(compact.get("extraction"))

    # WHAT A PASS DOES NOT MEAN, carried in the response rather than only in
    # the documentation. Nothing in this service can establish that an
    # identity document was issued by the authority it names: there is no
    # issuer API, no government lookup and no issuer signature to check. A
    # PASS therefore means "structurally valid and internally consistent",
    # and a caller who reads it as "genuine" has been misled by silence.
    if verification.get("authenticity"):
        compact["authenticity"] = verification["authenticity"]

    # Findings a reviewer should see that are NOT grounds to refuse the
    # document. Kept out of `reason_codes` on purpose: a code in that list
    # reads as a problem with the document, and these are not reliable enough
    # to be treated that way.
    if verification.get("advisories"):
        compact["advisories"] = list(verification["advisories"])

    # Present only for a specialist-handled document, and only what a caller
    # must know to READ the verdict: which capability judged it, what it
    # decided, why, and what it refused to claim. The capability's internal
    # checks, confidences and field scores stay internal.
    if specialist:
        # The BUSINESS verdict, not how it was reached. `capability`,
        # `input_mode` and `document_type` named the service, the calling
        # convention and the internal routing key -- three ways of saying
        # which code ran, which is this service's business and not the
        # client's.
        detail: dict[str, Any] = {
            "decision": specialist.get("decision"),
            "reason_codes": specialist.get("reason_codes") or [],
        }

        # `subtype` stays: a gift deed is not a sale deed, and a client
        # holding it as collateral needs to know which one it has.
        if specialist.get("subtype") is not None:
            detail["subtype"] = specialist["subtype"]

        if specialist.get("signature"):
            detail["signature"] = specialist["signature"]

        for claim in (
            "ownership_verified",
            "authenticity_verified",
            "business_existence_verified",
        ):
            if claim in specialist:
                detail[claim] = specialist[claim]

        compact["specialist"] = detail

    # The caller's asserted type, when classification was not allowed to run.
    # Carried as `hint` so it is never mistaken for a type this service found.
    hint = (document.get("document") or {}).get("hint")
    if hint:
        compact["hint"] = hint

    # What the caller said this file was, whenever they said anything. On a
    # DOCUMENT_TYPE_MISMATCH this is the half of the story `type` cannot
    # tell: the response has to show what was asked for AND what arrived.
    expected_type = document.get("expected_type")
    if expected_type:
        compact["expected_type"] = expected_type

    refs = _public_evidence_refs(document)
    if refs:
        compact["evidence_refs"] = refs

    errors = document.get("errors") or []
    if errors:
        compact["errors"] = errors

    return compact


# ==========================================================================
# PARTY SECTIONS -- the same results, grouped by whose they are
# ==========================================================================

#: Verdict -> the counter it increments in a party's summary.
#:
#: FAILED and REJECTED are counted together under `failed`: one means the
#: file could not be processed and the other that it was processed and
#: refused, and a caller triaging a queue treats both the same way. The
#: distinction survives untouched on each document's own `verification`.
_SUMMARY_BUCKETS = {
    "PASS": "passed",
    "REVIEW": "review",
    "FAIL": "failed",
    "FAILED": "failed",
    "REJECTED": "failed",
    "SKIPPED": "skipped",
}


def verification_summary(documents: list[dict[str, Any]]) -> dict[str, int]:
    """
    How one party's documents came out, counted.

    DERIVED, NEVER DECIDED. Every number here is a tally of verdicts that
    verification already reached. Nothing in this function can change a
    document's outcome, and the counts always sum to `total_documents` --
    an unrecognised verdict lands in `skipped` rather than vanishing,
    because a summary that quietly loses a document is worse than one
    that files it under the wrong heading.

    Counted over the COMPACT documents, which is what the caller sees, so
    the summary and the list beneath it can never disagree.
    """
    summary = {"total_documents": len(documents), "passed": 0,
               "review": 0, "failed": 0, "skipped": 0}

    for document in documents:
        verdict = str(document.get("verification") or "SKIPPED").upper()
        summary[_SUMMARY_BUCKETS.get(verdict, "skipped")] += 1

    return summary


def party_section(
    *,
    party_id: str,
    party_role: str,
    documents: list[dict[str, Any]],
    profile_match: dict[str, Any] | None = None,
    kyc: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """
    One party's slice of the response: whose, what they sent, how it went.

    A REGROUPING, NOT A SECOND RESULT. `documents` are the very same
    compact dicts published in the top-level `documents[]`, and
    `profile_match` is the entry Phase 4 already produced. Nothing is
    recomputed, so the two views cannot drift into disagreeing about the
    same document.

    `profile_match` appears only when that party had a profile to match.
    An empty object would read as "we matched and found nothing", which
    is a different and much stronger claim than "nobody told us who this
    person is".
    """
    section: dict[str, Any] = {
        "party_id": party_id,
        "role": party_role,
        # THIS PARTY'S DOCUMENT AND KYC STATE. Not a decision, and
        # deliberately not a `next_action`: a party-level CONTINUE beside
        # a case-level MANUAL_REVIEW would read as permission to proceed,
        # and there is ONE decision on a loan. `decision` and
        # `next_action` stay at case level, where they are true.
        **({"status": status} if status else {}),
        # REFERENCES, NOT COPIES. The full objects are published once in
        # the top-level `documents[]`, each already carrying its
        # `party_id`; repeating them here put every document in the
        # response twice and made half the payload a second copy that
        # could only ever agree with the first. `source_id` is the
        # caller's own filename and the stable handle they sent.
        "document_ids": [str(d.get("source_id")) for d in documents
                         if d.get("source_id")],
        "verification_summary": verification_summary(documents),
    }

    if profile_match:
        section["profile_match"] = profile_match

    # THIS PARTY'S OWN CROSS-DOCUMENT KYC -- do their documents describe
    # one person? Never compared against the other party's documents:
    # two people disagreeing is what a joint application IS.
    if kyc:
        section["kyc"] = public_kyc(kyc)

    return section


def public_kyc(kyc: dict[str, Any] | None, *,
               compact: bool = False) -> dict[str, Any]:
    """
    One KYC verdict, as a caller sees it.

    An allowlist, shared by the case-level object and the party sections
    so the two cannot describe the same result differently. `checks`
    stays out: the cross-document view already publishes them, and
    repeating them here would put the same codes in the response twice.

    `compact` drops the field rows. Used for the CASE-LEVEL object on a
    two-party case, where every row is already published under the party
    it belongs to -- keeping both put the whole of both parties' KYC in
    the response twice, and the copy at the top could not say whose row
    was whose without an extra key. The verdict, the score, the
    confidence and the reason codes stay, because those are the
    case-level answer and are not repeated anywhere else.
    """
    kyc = kyc or {}

    published = {
        "status": kyc.get("status", "SKIPPED"),
        "reason_codes": list(kyc.get("reason_codes") or []),
        "overall_score": kyc.get("overall_score", 0),
        "overall_confidence": kyc.get("overall_confidence", 0),
    }

    if not compact:
        published["fields"] = public_kyc_fields(kyc)

    return published


def public_document(document: dict[str, Any]) -> dict[str, Any]:
    """
    One document, in the client-facing shape.

    Normalises the two internal shapes -- the Document Agent's and a
    specialist's -- onto one contract, so a caller does not have to know
    which path a file took to read its result.
    """
    verification = document.get("verification") or {}
    specialist = document.get("specialist") or {}
    extraction = document.get("extraction")

    # A specialist reports `decision`; the Document Agent reports `status`.
    # The client gets one word for one concept.
    status = verification_status(document)

    reason_codes = list(
        verification.get("reason_codes") or specialist.get("reason_codes") or []
    )

    public: dict[str, Any] = {
        "source_id": document.get("source_id"),
        # The per-document roll-up. Existing clients and tests read this, and
        # it answers "how did this one file end up" without them having to
        # combine verification and extraction themselves.
        "status": document.get("status"),
        "document": document.get("document") or {},
        "verification": {
            "status": status,
            "reason_codes": reason_codes,
        },
        "extraction": _public_detailed_extraction(extraction, status),
        "processing": _public_timings(document),
        "errors": document.get("errors") or [],
    }

    refs = _public_evidence_refs(document)
    if refs:
        public["evidence_refs"] = refs

    # A specialist's own verdict is worth surfacing, but compactly: the
    # verdict, why, and what it refused to claim -- not its internal checks.
    if specialist:
        public["specialist"] = {
            "capability": specialist.get("capability"),
            "decision": specialist.get("decision"),
            "reason_codes": specialist.get("reason_codes") or [],
        }

        # Which variant handled it: the proof slot, the signature input mode,
        # the deed subtype. A caller needs these to read the verdict -- a
        # signature REVIEW means something different standalone than embedded.
        for detail in ("document_type", "input_mode", "subtype", "template"):
            if specialist.get(detail) is not None:
                public["specialist"][detail] = specialist[detail]

        # The signature block answers the four questions a caller actually
        # asks, and is already compact.
        if specialist.get("signature"):
            public["specialist"]["signature"] = specialist["signature"]

        # What the capability refused to claim, carried through so nobody has
        # to infer it from absence.
        for claim in (
            "ownership_verified",
            "authenticity_verified",
            "business_existence_verified",
        ):
            if claim in specialist:
                public["specialist"][claim] = specialist[claim]

    return public



# A capability is handed a file staged inside the upload sandbox, so the
# locator it reports names that staged file -- "los_<uuid>.png#0,136,640,166".
# By the time the response is built the file has been unlinked, so the client
# receives a path that is useless to it, points at nothing, and leaks the
# sandbox naming scheme. The stable name for the same thing is the caller's
# own source_id, which is what they uploaded it as.
_INTERNAL_NAME_RE = re.compile(r"^los_[0-9a-f]{8,}\.[A-Za-z0-9]+$")

#: Anything that looks like a filesystem path rather than a logical reference.
_PATH_HINT_RE = re.compile(r"[\\/]|^[A-Za-z]:")


def _public_locator(locator: str, source_id: str) -> str:
    """
    One evidence locator, as a reference the caller can actually resolve.

    The fragment is what carries the meaning -- a page number, a pixel region
    -- and it is kept. Only the part that names a file is replaced, and only
    when that part is an internal artefact rather than something the caller
    would recognise.
    """
    text = str(locator or "").strip()
    if not text:
        return source_id

    head, separator, fragment = text.partition("#")

    # "page=1" with no file part: a bare fragment, so read it as one.
    if not separator and ("=" in head or "," in head):
        head, fragment = "", head

    if not head or _INTERNAL_NAME_RE.match(head) or _PATH_HINT_RE.search(head):
        head = source_id

    if not fragment:
        return head

    # A bare pixel region reads as noise without saying what it is.
    if re.fullmatch(r"\d+(?:,\d+){3}", fragment):
        fragment = f"region={fragment}"

    return f"{head}#{fragment}"


def _public_evidence_refs(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Evidence references, with no internal file naming in them."""
    source_id = str(document.get("source_id") or "")

    public: list[dict[str, Any]] = []
    for ref in document.get("evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        # Rebuilt rather than copied. `detail` carried the capability's own
        # wording -- "region via darkest_band" names an internal heuristic --
        # so a reference that is meant to be stable was shipping an
        # implementation note that could change with any tuning pass.
        entry_source = str(ref.get("source_id") or source_id)
        public.append({
            "source_id": entry_source,
            "locator": _public_locator(ref.get("locator", ""), entry_source),
        })

    return public


def released_extraction(
    extraction: dict[str, Any] | None,
    verification_status: str,
) -> dict[str, Any] | None:
    """
    Extraction, released only behind the verification gate.

    THE ONE PLACE extracted fields escape the internal envelope, so the
    gate is enforced here and nowhere else. Public rather than private
    because profile matching consumes released fields too, and reusing
    this is the difference between obeying the gate and re-deciding it. It was briefly written twice -- once here and once in
    the compact shape -- and the second copy did not have the gate, so a
    specialist REVIEW handed its fields back anyway. One gate, one place.

    Two independent conditions, in order:

      verification must have PASSed. SKIPPED is not a pass: a check that did
      not run has established nothing, and reading it as permission would
      make switching verification off silently WIDEN what the API releases.

      extraction must be switched on. Off means the fields are withheld even
      from a document that passed, because the operator asked for that.
    """
    if str(verification_status).upper() != "PASS":
        return None

    from app.agents.los import config as los_config

    if not los_config.extraction_enabled():
        return None

    if not extraction:
        return None

    fields = extraction.get("fields") or {}
    return {
        "status": extraction.get("status") or ("SUCCESS" if fields else "PARTIAL"),
        "fields": fields,
    }


def _public_detailed_extraction(
    extraction: dict[str, Any] | None, status: str,
) -> dict[str, Any] | None:
    """The detailed shape's extraction, behind the gate and cleaned."""
    released = released_extraction(extraction, status)
    if not released:
        return None
    return {**released,
            "fields": public_extraction_values(released.get("fields"))}


def _public_timings(document: dict[str, Any]) -> dict[str, float]:
    """
    Stage timings, flattened.

    The Document Agent nests some of these under `ocr` and `classification`
    objects that also carry confidences; only the durations belong in a
    client response.
    """
    processing = document.get("processing") or {}

    timings = {
        key: round(float(processing.get(key) or 0.0), 2)
        for key in (
            "classification_ms",
            "verification_ms",
            "extraction_ms",
            "total_ms",
        )
    }

    # Attribution for a specialist call: what MCP measured, and what routing
    # cost around it. Present only where a specialist ran, and never summed
    # with specialist_ms -- they are nested, not additive.
    for key in ("specialist_ms", "mcp_ms", "orchestration_ms"):
        if processing.get(key) is not None:
            timings[key] = round(float(processing[key]), 2)

    # A specialist reports one duration for the whole capability; without
    # this its timings would all read zero.
    specialist_ms = processing.get("specialist_ms")
    if specialist_ms and not timings["total_ms"]:
        timings["extraction_ms"] = round(float(specialist_ms), 2)
        timings["total_ms"] = round(float(specialist_ms), 2)

    # Always present, including as 0.0. A specialist that never OCR'd and a
    # document whose OCR time went unrecorded are different things, and a
    # missing key forces every caller to guard for it.
    timings["ocr_ms"] = round(float(processing.get("ocr_ms") or 0.0), 2)

    return timings


__all__ = [
    "conflicts_from_kyc", "cross_document_from", "decision_from",
    "public_kyc_fields",
    "public_document",
    "verification_status", "any_document_verified", "released_extraction",
    "party_section", "verification_summary", "public_kyc", "jsonable",
    "public_extraction_values",
    "NO_VERIFIED_DOCUMENTS", "DOCUMENT_TYPE_MISMATCH",
]
