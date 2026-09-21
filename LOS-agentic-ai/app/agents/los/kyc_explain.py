"""
What the KYC numbers MEAN, said in words.

THE PROBLEM THIS SOLVES. A response carrying `overall_score: 38` invites
exactly one reading -- "this applicant is 38% accurate" -- and that is not
what the number is. `fields.roll_up` computes a weighted mean over the
fields that were ACTUALLY COMPARED, so on a bundle where only the name
could be compared, 38 is the name-match score wearing a general-sounding
name. A reviewer acting on it as an accuracy figure is acting on something
that does not exist.

NOTHING HERE COMPUTES A SCORE. Every number published by this module is
read from a number KYC already produced. The additions are a label, a
band and a sentence -- explanation, not evaluation. If this module were
deleted the verdict would be identical, which is the property that makes
it safe to add.

NO MODEL, NO NETWORK, NO I/O. Every sentence below is selected by a
reason code through a lookup table. An LLM writing these would produce a
fluent explanation of a case it had not read, and a wrong sentence beside
a right score is worse than no sentence.
"""

from __future__ import annotations

from typing import Any

#: Which field a single-field score actually measured. The FieldResult
#: names are the internal ones; these are what a reader calls them.
_FIELD_SCORE = {
    "NAME": ("NAME_MATCH", "Name Match Score"),
    "DATE_OF_BIRTH": ("DOB_MATCH", "Date of Birth Match Score"),
    "ADDRESS": ("ADDRESS_MATCH", "Address Match Score"),
    "PAN_NUMBER": ("PAN_MATCH", "PAN Match Score"),
    "FATHER_NAME": ("FATHER_NAME_MATCH", "Father's Name Match Score"),
    "INCOME": ("INCOME_CONSISTENCY", "Income Consistency Score"),
}

#: What the score is called when more than one field went into it. It is
#: a consistency figure across documents -- NEVER an accuracy figure, and
#: deliberately not named one: nothing in this pipeline measures how often
#: the extractor is right, and a field called "accuracy" would be read as
#: though something did.
_COMPOSITE = ("KYC_CONSISTENCY", "KYC Consistency Score")

#: Bands for the human reading. EXPLANATORY ONLY -- no PASS/FAIL boundary
#: is drawn here. The thresholds that decide a case live in
#: kyc_policies.yaml and are untouched by this module, so a band and a
#: verdict can legitimately disagree: a non-blocking check may FAIL and
#: still leave the case at REVIEW.
_BANDS = (
    (100, "Exact match"),
    (90, "Very high similarity"),
    (75, "High similarity"),
    (50, "Moderate similarity"),
    (25, "Low similarity"),
    (0, "Very low similarity"),
)

#: One sentence per reason code. Only codes whose meaning is unambiguous
#: from the code alone appear here -- a sentence guessed from a code that
#: does not carry the fact is a fabrication with a citation.
_FIELD_MESSAGE = {
    "NAME_MISMATCH":
        "The names found on the available documents do not sufficiently match.",
    "NAME_PARTIAL_MATCH":
        "The names are similar but not close enough to confirm automatically.",
    "NAME_SINGLE_SOURCE":
        "Only one name source was available, so cross-document matching "
        "could not be performed.",
    "NAME_MISSING":
        "No name was available from the verified documents.",
    "DOB_MISMATCH":
        "The dates of birth on the available documents do not match.",
    "DOB_SINGLE_SOURCE":
        "Only one document carried a date of birth, so it could not be "
        "cross-checked.",
    "DOB_MISSING":
        "No date of birth was available from the verified documents.",
    "ADDRESS_MISMATCH":
        "The addresses on the available documents do not sufficiently match.",
    "ADDRESS_PARTIAL_MATCH":
        "The addresses are similar but not close enough to confirm "
        "automatically.",
    "ADDRESS_SINGLE_SOURCE":
        "Only one address-bearing document was available, so the address "
        "could not be cross-checked.",
    "ADDRESS_MISSING":
        "No address was available from the verified documents.",
    "ADDRESS_NOT_COMPARABLE":
        "The addresses had no component in common that could be compared.",
    "PAN_MISMATCH":
        "The PAN numbers on the available documents do not match.",
    "PAN_SINGLE_SOURCE":
        "Only one document carried a PAN, so it could not be cross-checked.",
    "PAN_MISSING":
        "No PAN was available from the verified documents.",
    "PAN_INVALID_FORMAT":
        "A PAN was read but does not have a valid format.",
    "FATHER_NAME_MISMATCH":
        "The father's names on the available documents do not match.",
    "FATHER_NAME_PARTIAL_MATCH":
        "The father's names are similar but not close enough to confirm "
        "automatically.",
    "FATHER_NAME_SINGLE_SOURCE":
        "Only one document carried a father's name, so it could not be "
        "cross-checked.",
    "FATHER_NAME_MISSING":
        "No father's name was available from the verified documents.",
    "INCOME_MISSING":
        "No income figures were available from the verified documents.",
    "INCOME_SINGLE_SOURCE":
        "Only one income source was available, so it could not be "
        "cross-checked.",
    "INCOME_MISMATCH":
        "The income figures across the documents differ materially.",
    "INSUFFICIENT_SOURCES":
        "There was not enough comparable information across the documents "
        "to reach a conclusion.",
    "KYC_DISABLED":
        "Cross-document identity checking is switched off.",
}

#: Title and next step per verdict. The action names what a human does,
#: not what the system decided -- and never approves anything.
_VERDICT = {
    "PASS": ("Identity verified across documents", "No action required"),
    "REVIEW": ("Identity verification requires review", "Manual review required"),
    "FAIL": ("Identity verification failed", "Manual review required"),
    "SKIPPED": ("Identity could not be cross-checked", "No action required"),
}

#: How a document type reads in a sentence.
_DOCUMENT_NAME = {
    "PAN": "PAN",
    "DRIVING_LICENCE": "driving licence",
    "VOTER_ID": "voter ID",
    "PASSPORT": "passport",
    "BANK_STATEMENT": "bank account holder name",
    "SALARY_SLIP": "salary slip",
    "ITR": "income tax return",
}

#: Severity, worst last -- for picking the issue a reviewer reads first.
_SEVERITY = {"PASS": 0, "SKIPPED": 1, "REVIEW": 2, "FAIL": 3}


def _band(value: int) -> str:
    for floor, text in _BANDS:
        if value >= floor:
            return text
    return _BANDS[-1][1]


#: How a party reads in a sentence and in a score type.
_ROLE_WORD = {
    "PRIMARY_APPLICANT": ("Primary applicant", "PRIMARY_APPLICANT"),
    "CO_APPLICANT": ("Co-applicant", "CO_APPLICANT"),
}


def _compared(kyc: dict[str, Any],
              party_id: str | None = None) -> list[dict[str, Any]]:
    """
    The field rows that actually went into the score.

    `party_id` narrows to one person's rows. THIS MATTERS AT CASE LEVEL:
    the aggregate merges both parties' rows, so counting them all made a
    score that is a MINIMUM over parties look like a mean over many
    fields -- and one co-applicant's name-match score was published as
    "KYC Consistency" for the whole case.
    """
    rows = [row for row in (kyc.get("fields") or [])
            if str(row.get("status") or "").upper() != "SKIPPED"]
    if party_id:
        rows = [row for row in rows if row.get("party_id") == party_id]
    return rows


def _basis_party(kyc: dict[str, Any]) -> tuple[str, str] | None:
    """
    Whose score `overall_score` is, on a case-level aggregate.

    None on a single party's own payload, where the score is simply
    theirs and needs no attribution.
    """
    party_id = kyc.get("score_basis_party_id")
    if not party_id:
        return None
    role = str(kyc.get("score_basis_party_role") or "PRIMARY_APPLICANT")
    return str(party_id), role


def score_basis(kyc: dict[str, Any]) -> str | None:
    """
    The name of what `overall_score` measured, party included.

    `CO_APPLICANT_NAME_MATCH` rather than a bare `NAME_MATCH`, because
    at case level the reader's first question is whose number it is.
    """
    published = score_of(kyc)
    return published["type"] if published else None


def score_of(kyc: dict[str, Any]) -> dict[str, Any] | None:
    """
    The existing score, named and banded.

    RETURNS None WHEN NOTHING WAS COMPARED. `roll_up` returns 0 in that
    case, and its docstring is explicit that this is not a score of zero
    in the sense of "everything disagreed". Publishing `{value: 0,
    interpretation: "Very low similarity"}` would assert exactly the
    thing the algorithm refuses to assert. The absence of the object is
    the honest answer; `status` and the reason codes already carry why.
    """
    basis = _basis_party(kyc)

    # AT CASE LEVEL THE SCORE IS ONE PARTY'S. `_case_kyc` publishes the
    # MINIMUM of the parties' scores, so the basis is whichever person
    # that minimum came from -- and only THEIR rows may be counted when
    # deciding what it measured.
    compared = _compared(kyc, basis[0] if basis else None)
    if not compared:
        return None

    if len(compared) == 1:
        field = str(compared[0].get("field") or "").upper()
        kind, label = _FIELD_SCORE.get(field, _COMPOSITE)
    else:
        kind, label = _COMPOSITE

    if basis:
        word, prefix = _ROLE_WORD.get(basis[1], ("Applicant", "APPLICANT"))
        kind = f"{prefix}_{kind}"
        label = f"{word} {label}"

    value = int(kyc.get("overall_score") or 0)

    return {
        "value": value,
        "type": kind,
        "label": label,
        "interpretation": _band(value),
        "confidence": int(kyc.get("overall_confidence") or 0),
    }


def verification_summary(kyc: dict[str, Any]) -> dict[str, Any] | None:
    """
    How many checks ran, and how they came out.

    Counted from the check rows KYC already produced. `checks_review` is
    carried when any check reached REVIEW so that passed + failed +
    review + skipped always equals the total -- a summary whose numbers
    do not add up invites the reader to assume the missing ones failed.
    """
    checks = kyc.get("checks") or []
    if not checks:
        return None

    summary: dict[str, Any] = _tally(checks)

    # EACH PARTY'S OWN NUMBERS, where the case has more than one.
    # "three of four checks passed" across two people describes neither
    # of them: the primary passed everything and the co-applicant failed
    # a name match, and a reviewer needs to see which is which. The
    # case-wide counts stay beside them, unchanged.
    for party in kyc.get("per_party") or []:
        role = str(party.get("party_role") or "").lower()
        if role in {"primary_applicant", "co_applicant"}:
            summary[role] = {
                **_tally(party.get("checks") or []),
                "party_id": party.get("party_id", ""),
            }

    issue = _primary_issue(checks)
    if issue:
        summary["primary_issue"] = issue

    return summary


def _tally(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts over one set of checks."""
    counts: dict[str, int] = {}
    for check in checks:
        status = str(check.get("status") or "SKIPPED").upper()
        counts[status] = counts.get(status, 0) + 1

    skipped = counts.get("SKIPPED", 0)
    tallied: dict[str, Any] = {
        "checks_evaluated": len(checks) - skipped,
        "checks_passed": counts.get("PASS", 0),
        "checks_failed": counts.get("FAIL", 0),
        "checks_skipped": skipped,
    }
    if counts.get("REVIEW"):
        tallied["checks_review"] = counts["REVIEW"]
    return tallied


def _worst_check(
    checks: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]] | None:
    """
    The check a reviewer should read first, and the row it came from.

    THE ROW IS RETURNED, NOT JUST THE CODE. The sentence has to name the
    party and the documents the failure actually involved -- and those
    live on the check, not on the code. Reading the code alone is what
    produced "the PAN does not match the driving licence" on a case
    whose PAN and driving licence agreed perfectly.
    """
    worst: tuple[int, str, dict[str, Any]] | None = None

    for check in checks:
        status = str(check.get("status") or "SKIPPED").upper()
        rank = _SEVERITY.get(status, 1)
        if rank < 2:                     # PASS and SKIPPED are not issues
            continue
        for code in check.get("reason_codes") or []:
            if worst is None or rank > worst[0]:
                worst = (rank, str(code), check)
            break

    return (worst[1], worst[2]) if worst else None


def _primary_issue(checks: list[dict[str, Any]]) -> str | None:
    """The one reason code a reviewer should read first."""
    found = _worst_check(checks)
    return found[0] if found else None


def _documents_in(kyc: dict[str, Any], field_name: str,
                  party_id: str | None = None) -> list[str]:
    """
    What the named field was compared across, as a person says it.

    PARTY-FILTERED. On a joint case both people have a NAME row; taking
    the first one described the primary applicant's documents while the
    failure belonged to the co-applicant's.
    """
    for row in kyc.get("fields") or []:
        if str(row.get("field") or "").upper() != field_name:
            continue
        if party_id and row.get("party_id") != party_id:
            continue
        names: list[str] = []
        for source in row.get("sources") or []:
            readable = _DOCUMENT_NAME.get(
                str(source.get("document_type") or "").upper())
            if readable and readable not in names:
                names.append(readable)
        return names
    return []


def _role_of(kyc: dict[str, Any], party_id: str | None) -> str | None:
    """Which role a party_id belongs to, from the payload's own record."""
    if not party_id:
        return None
    for party in kyc.get("per_party") or []:
        if party.get("party_id") == party_id:
            return str(party.get("party_role") or "")
    return None


def result_of(kyc: dict[str, Any]) -> dict[str, Any]:
    """
    The verdict as a sentence a reviewer can act on.

    IT NAMES THE FAILURE THAT ACTUALLY HAPPENED. The sentence is built
    from the worst check's own row -- its party and its documents -- not
    from the reason code alone. Reading the code alone produced "the PAN
    does not match the driving licence" on a case where the PAN and the
    driving licence agreed and the real mismatch was the co-applicant's
    bank statement: a true code turned into a false sentence by being
    attributed to the wrong person.

    Nothing is inferred, and a code with no entry in `_FIELD_MESSAGE`
    produces no sentence rather than a plausible one.
    """
    status = str(kyc.get("status") or "SKIPPED").upper()
    title, action = _VERDICT.get(
        status, ("Identity check completed", "Manual review required"))

    checks = kyc.get("checks") or []
    found = _worst_check(checks)

    issue = found[0] if found else None
    failing = found[1] if found else {}

    if not issue:
        for code in kyc.get("reason_codes") or []:
            issue = str(code)
            break

    party_id = failing.get("party_id")
    role = _role_of(kyc, party_id)
    joint = len(kyc.get("per_party") or []) > 1

    message = _FIELD_MESSAGE.get(issue or "")

    # Name the documents where the payload identifies them, so "the
    # names do not match" becomes "the PAN name does not match the bank
    # account holder name" -- read from the FAILING party's own row.
    #: The clause, lower-cased and unpunctuated, so it can either stand
    #: as its own sentence or be embedded after "because".
    clause: str | None = None

    if issue in {"NAME_MISMATCH", "NAME_PARTIAL_MATCH"}:
        names = _documents_in(kyc, "NAME", party_id)
        if len(names) == 2:
            verb = ("does not match" if issue == "NAME_MISMATCH"
                    else "does not closely match")
            clause = (f"the {names[0]} name {verb} the {names[1]}"
                      if names[1].endswith("name")
                      else f"the name on the {names[0]} {verb} the name "
                           f"on the {names[1]}")
            if not joint:
                message = f"The applicant's {clause[4:]}."

    if joint and role:
        # WHOSE PROBLEM IT IS, FIRST. On a joint application the reader's
        # opening question is which of the two people this is about.
        word = _ROLE_WORD.get(role, ("Applicant", "APPLICANT"))[0]
        title = ("KYC review required" if status in {"REVIEW", "FAIL"}
                 else title)
        detail = clause
        if detail is None and message:
            detail = (message[0].lower() + message[1:]).rstrip(".")
        if detail:
            verdict = "failed" if status == "FAIL" else "requires review"
            message = (f"{word} identity verification {verdict} because "
                       f"{detail}.")

    published: dict[str, Any] = {"title": title, "action": action}
    if message:
        published["message"] = message
    return published


def field_message(reason_code: str | None) -> str | None:
    """The sentence for one field row, where its code carries the fact."""
    if not reason_code:
        return None
    return _FIELD_MESSAGE.get(str(reason_code).upper())


__all__ = ["field_message", "result_of", "score_basis", "score_of",
           "verification_summary"]
