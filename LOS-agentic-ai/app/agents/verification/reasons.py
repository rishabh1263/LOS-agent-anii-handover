"""
A sentence for every reason code, whichever path produced it.

THE RULE THIS ENFORCES: every REVIEW, FAIL and REJECTED carries both a
machine-readable code and a sentence a person can act on. A code routes a
queue; it does not tell the field officer standing in front of a customer
what to do next. `PARTY_MISSING` is precise and means nothing to them.

WHY IT IS CENTRAL AND NOT PER-PATH. Reason codes are produced in at least
five places -- the structural gates, the post-extraction rules, the image
quality analyser, the financial verifiers and the specialist capabilities
-- and each grew its own idea of whether to include prose. Bank statements
carried sentences; sale deeds carried four codes and no sentences at all;
identity documents carried sentences only for image quality. A caller
could not rely on the field being there, so a UI could not render it.

TWO LAYERS, DELIBERATELY:

1.  THE CATALOGUE, below. A written sentence for every code this service
    is known to emit. A test enumerates the real emitters -- the enums
    and modules themselves, not a grep -- and fails when one has no
    entry, so adding a code without explaining it does not get past CI.

2.  A DERIVED FALLBACK. A code with no entry still produces a readable
    sentence from its own name. This is the safety net, never the plan: a
    derived sentence is grammatical and shallow, and the test above
    exists so that nothing ships relying on it.

WHAT THIS MODULE DOES NOT DO. It does not decide anything. It never sees a
verdict, cannot change one, and adding a code here has no effect on any
gate. It turns an identifier into English.
"""

from __future__ import annotations

import re

#: code -> the sentence a field officer reads.
#:
#: Written in the second person where there is an action to take, and
#: neutrally where there is not. None of them states a cause the service
#: has not established: "could not be read" rather than "is forged".
CATALOGUE: dict[str, str] = {
    # -- structural gates ---------------------------------------------
    "DOCUMENT_UNREADABLE": (
        "Too little text could be read from this document. Re-upload a "
        "clearer copy."
    ),
    "DOCUMENT_TYPE_MISMATCH": (
        "This is not the type of document that was expected. Upload the "
        "correct document."
    ),
    "DOCUMENT_CLASS_UNKNOWN": (
        "The type of this document could not be identified."
    ),
    "IDENTIFIER_NOT_FOUND": (
        "The identifying number could not be found on this document."
    ),
    "IDENTIFIER_FORMAT_INVALID": (
        "The identifying number on this document is not in the expected "
        "format."
    ),
    "FIELD_FORMAT_INVALID": (
        "A value read from this document is not in the expected format."
    ),
    "POOR_SCAN_QUALITY": (
        "The text on this document was read with low confidence."
    ),
    "OCR_LOW_CONFIDENCE": (
        "The text on this document was read with low confidence."
    ),
    "SIGNATURE_NOT_FOUND": "No signature was found on this document.",
    "SIGNATURE_CAPTION_MISSING": (
        "The signature caption could not be read on this document."
    ),
    "BLANK_PAGE": "This page appears to be blank.",

    # -- post-extraction rules ----------------------------------------
    "REQUIRED_FIELD_MISSING": (
        "A field this document must carry could not be read from it."
    ),
    "REQUIRED_FIELD_NOT_FOUND": (
        "A field this document must carry could not be read from it."
    ),
    "DOCUMENT_EXPIRED": "This document has expired.",
    "DOCUMENT_EXPIRING_SOON": "This document expires soon.",
    "DATE_IN_FUTURE": "A date on this document is in the future.",
    "DOB_IN_FUTURE": "The date of birth on this document is in the future.",
    "DOB_IMPLAUSIBLE": (
        "The date of birth on this document implies an implausible age."
    ),
    "APPLICANT_UNDER_18": (
        "The date of birth on this document implies the holder is under 18."
    ),
    "PAN_STRUCTURE_INVALID": (
        "The PAN on this document is not structurally valid."
    ),
    "PAN_NAME_INITIAL_MISMATCH": (
        "The PAN's holder-type letter does not match the printed name."
    ),

    # -- image quality --------------------------------------------------
    #
    # Every one of these describes the PHOTOGRAPH, not the document, and
    # says what to do about it. A blurred picture of a real card is a real
    # card.
    "DOCUMENT_IMAGE_BLURRY": (
        "The photograph is blurred, which may affect what can be read."
    ),
    "DOCUMENT_IMAGE_SEVERELY_BLURRY": (
        "The photograph is too blurred to read. Retake it with the camera "
        "steady and the document in focus."
    ),
    "LOW_IMAGE_RESOLUTION": (
        "The image is too small for the print to be read. Photograph the "
        "document closer, or upload a larger scan."
    ),
    "LOW_TEXT_CONTRAST": (
        "The print is faint against the background."
    ),
    "IMAGE_TOO_DARK": (
        "The photograph is dark, which may affect what can be read."
    ),
    "IMAGE_OVEREXPOSED": (
        "The photograph is very bright, which may hide some print."
    ),
    "SEVERE_GLARE": (
        "A reflection on the document may be hiding some print."
    ),
    "DOCUMENT_ROTATED": "The document is tilted in the frame.",
    "DOCUMENT_PARTIALLY_CROPPED": (
        "Part of the document may be cut off. Retake the photograph with "
        "the whole document inside the frame."
    ),
    "DOCUMENT_IMAGE_NOISY": "The image is grainy.",

    # -- the seven outcomes that must stay distinct --------------------
    #
    # See PHASE 2B in the brief, and `DISTINCT_OUTCOMES` below. Each of
    # these says something different about why an answer was not reached,
    # and the operator response to each is different.
    "VERIFICATION_TIMEOUT": (
        "This document could not be read all the way through within the "
        "time allowed. This is a limit of the service, not a finding about "
        "the document."
    ),
    "BANK_STATEMENT_PARSE_INCOMPLETE": (
        "This document could not be read all the way through, so some of "
        "its contents are missing."
    ),
    "BANK_STATEMENT_NO_TRANSACTIONS": (
        "No transactions could be read from this bank statement, so its "
        "structure could not be confirmed."
    ),
    "DOCUMENT_REQUIRES_OCR": (
        "This document is a scan that could not be read automatically. "
        "Re-upload it as a clearer scan, or have it reviewed."
    ),
    "DOCUMENT_REQUIRES_LANGUAGE_OCR": (
        "This document is in a script this service cannot read "
        "automatically. It needs to be reviewed."
    ),
    "BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE": (
        "This bank statement needs review because its transaction "
        "integrity could not be established confidently."
    ),
    "BANK_STATEMENT_RECONCILIATION_FAILED": (
        "The transactions on this statement do not add up to the balance "
        "it shows."
    ),
    "BANK_STATEMENT_STRUCTURE_UNCLEAR": (
        "The opening and closing balances could not both be read, so this "
        "statement needs review."
    ),
    "VERIFICATION_INCONCLUSIVE": (
        "This document needs review because its verification could not be "
        "completed."
    ),
    "INVALID_DOCUMENT": "This file is not a document this service can read.",

    # -- sale deed ------------------------------------------------------
    "FILE_UNREADABLE": "This file could not be opened.",
    "ESTAMP_PAGE_NOT_FOUND": (
        "The e-stamp page could not be found in this document."
    ),
    "REGISTRATION_REFERENCE_MISSING": (
        "No registration reference was found on this deed."
    ),
    "REGISTRATION_REFERENCE_MALFORMED": (
        "The registration reference on this deed is not in the expected "
        "format."
    ),
    "PARTY_MISSING": (
        "The parties to this deed could not be read from it."
    ),
    "PARTY_CONTAINS_LABEL": (
        "A party name on this deed was read together with its caption and "
        "could not be separated."
    ),
    "CONSIDERATION_MISSING": (
        "The consideration amount could not be read from this deed."
    ),
    "DEED_BODY_NOT_READABLE": (
        "The body of this deed could not be read."
    ),
    "OWNERSHIP_NOT_ESTABLISHED": (
        "Ownership could not be established from this document."
    ),
    "DEED_SUBTYPE_MISMATCH": (
        "This instrument is not the type of deed that was expected."
    ),
    "DEED_SUBTYPE_UNKNOWN": (
        "The type of this deed could not be identified."
    ),
    "DATE_ORDER_INVALID": (
        "The dates on this document are not in a possible order."
    ),
    "AMOUNT_INVALID": (
        "An amount on this document could not be read as a number."
    ),
    "PIN_CODE_INVALID": (
        "The PIN code on this document is not valid."
    ),

    # -- business existence / signature --------------------------------
    "BUSINESS_EXISTENCE_NOT_ESTABLISHED": (
        "The existence of the business could not be established from this "
        "document."
    ),
    "AUTHENTICITY_NOT_ESTABLISHED": (
        "This service cannot establish that the document was issued by the "
        "authority it names."
    ),
    "GEOTAG_MISSING": (
        "This photograph carries no location data."
    ),
    "REFERENCE_MISSING": (
        "There is no reference signature to compare this one against."
    ),

    # -- signature verification -----------------------------------------
    #
    # This capability reports POSITIVE codes as well as negative ones --
    # SIGNATURE_PRESENT and COMPARISON_MATCH appear on results that
    # passed. They are catalogued too: a code that reaches a caller as a
    # bare identifier is unreadable whether or not it is bad news.
    "SIGNATURE_PRESENT": "A signature was found on this document.",
    "SIGNATURE_ABSENT": "No signature was found where one was expected.",
    "SIGNATURE_BLANK": "The signature area on this document is empty.",
    "SIGNATURE_STRIP_BLANK": (
        "The signature strip on this document is empty."
    ),
    "SIGNATURE_REGION_NOT_FOUND": (
        "The part of the document that should carry the signature could "
        "not be located."
    ),
    "SIGNATURE_CROPPED": (
        "The signature runs past the edge of the image and may be "
        "incomplete."
    ),
    "SIGNATURE_LOW_QUALITY": (
        "The signature is too faint or too small to assess."
    ),
    "SIGNATURE_NOT_HANDWRITTEN": (
        "The mark in the signature area does not appear to be handwritten."
    ),
    "SIGNATURE_NOT_COMPARABLE": (
        "This signature cannot be compared with the reference one."
    ),
    "SIGNATURE_REFERENCE_MISSING": (
        "There is no reference signature on file to compare this one "
        "against."
    ),
    "SIGNATURE_SYNTHETIC_RISK": (
        "This signature shows characteristics of a generated image and "
        "needs review."
    ),
    "SIGNATURE_MANIPULATION_SUSPECTED": (
        "This signature shows signs of editing and needs review."
    ),
    "SIGNATURE_VERIFICATION_DISABLED": (
        "Signature verification is switched off in this deployment."
    ),
    "COMPARISON_MATCH": (
        "The signature matches the reference signature."
    ),
    "COMPARISON_MISMATCH": (
        "The signature does not match the reference signature."
    ),
    "COMPARISON_INCONCLUSIVE": (
        "The comparison with the reference signature was inconclusive."
    ),
    "QUALITY_INSUFFICIENT_FOR_COMPARISON": (
        "The image is not good enough to compare the signature reliably."
    ),
    "REFERENCE_UNAVAILABLE": (
        "The reference signature could not be retrieved."
    ),
    "REFERENCE_UNREADABLE": (
        "The reference signature could not be read."
    ),
    "RISK_SIGNALS_UNAVAILABLE": (
        "The risk checks for this signature could not be run."
    ),

    # -- shared image findings, as the specialists name them -------------
    #
    # The image-quality analyser and these capabilities describe the same
    # defects under different names. Both vocabularies are catalogued
    # rather than one being renamed: renaming a code is a contract change
    # for anyone already routing on it.
    "IMAGE_BLURRED": (
        "The image is blurred, which may affect what can be read."
    ),
    "IMAGE_TOO_SMALL": (
        "The image is too small to assess. Upload a larger one."
    ),
    "IMAGE_LOW_CONTRAST": (
        "The image has too little contrast to assess."
    ),
    "IMAGE_CLIPPED": (
        "Part of the image is cut off."
    ),
    "IMAGE_OVER_COMPRESSED": (
        "The image has been compressed too heavily to assess reliably."
    ),
    "UNSUPPORTED_DOCUMENT_TYPE": (
        "This document type is not one this check supports."
    ),
    "UNSUPPORTED_FILE_TYPE": (
        "This file type is not one this service can read."
    ),

    # -- business evidence photographs -----------------------------------
    "GEOTAG_PRESENT": "This photograph carries location data.",
    "GEOTAG_INVALID": (
        "The location data on this photograph could not be read."
    ),
    "TIMESTAMP_PRESENT": "This photograph carries a capture time.",
    "TIMESTAMP_MISSING": (
        "This photograph carries no capture time."
    ),
    "DEVICE_METADATA_PRESENT": (
        "This photograph carries camera information."
    ),
    "METADATA_STRIPPED": (
        "This photograph has had its metadata removed, so when and where "
        "it was taken cannot be established."
    ),

    # -- financial integrity ----------------------------------------------
    "VERIFICATION_INTEGRITY_FAILED": (
        "This document did not pass its integrity check."
    ),

    # -- pipeline / stage ------------------------------------------------
    "EXTRACTION_DISABLED": (
        "Field extraction is switched off in this deployment, so no fields "
        "were returned."
    ),
    "EXTERNAL_VERIFICATION_REQUIRED": (
        "This document type is verified through an external service and is "
        "not read here."
    ),
    "DOCUMENT_PROCESSING_ERROR": (
        "This document could not be processed."
    ),
    "NO_VERIFIED_DOCUMENTS": (
        "No document cleared verification, so no verified evidence supports "
        "this application."
    ),
}


#: THE SEVEN OUTCOMES THAT MUST NEVER BE CONFLATED.
#:
#: From the hardening brief, and each earned its place. The worst of them
#: in practice: a parser that ran out of time was reported as
#: BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE, so "we were too slow" read
#: as "this customer's statement may not add up". One sends a document to
#: a queue for more time; the other sends an accusation to a human.
#:
#: A test asserts these map to distinct sentences, so a future edit cannot
#: quietly collapse two of them into the same wording.
DISTINCT_OUTCOMES = (
    "VERIFICATION_TIMEOUT",
    "BANK_STATEMENT_PARSE_INCOMPLETE",
    "BANK_STATEMENT_NO_TRANSACTIONS",
    "INVALID_DOCUMENT",
    "BANK_STATEMENT_RECONCILIATION_INCONCLUSIVE",
    "DOCUMENT_REQUIRES_OCR",
    "DOCUMENT_REQUIRES_LANGUAGE_OCR",
)

_WORD = re.compile(r"[^A-Za-z0-9]+")


def derive(code: str) -> str:
    """
    A readable sentence from the code itself.

    THE SAFETY NET, NOT THE PLAN. Grammatical and shallow -- it can say
    "Party missing." and never "the parties to this deed could not be read
    from it". A code that relies on this is a code nobody has written an
    explanation for, which the catalogue test treats as a defect.
    """
    words = [w for w in _WORD.split(str(code or "")) if w]
    if not words:
        return "This document needs review."
    sentence = " ".join(words).lower().strip()
    return sentence[:1].upper() + sentence[1:] + "."


def explain(code: str) -> str:
    """The sentence for one code."""
    return CATALOGUE.get(str(code or "").upper()) or derive(code)


def explain_all(codes, existing=None) -> list[str]:
    """
    Sentences for a set of codes, preserving any the caller already wrote.

    `existing` wins. A verifier that produced a specific sentence knows
    more than this catalogue does -- it saw the document -- and a generic
    line must never displace a specific one. This fills gaps; it does not
    overwrite.

    De-duplicated because several codes legitimately share a sentence
    (REQUIRED_FIELD_MISSING and REQUIRED_FIELD_NOT_FOUND say the same
    thing), and a response repeating it twice reads like two problems.
    """
    # SUPPLIED MEANS "HAS SOMETHING IN IT", not "is a list".
    #
    # `if existing:` was true for `["", "   "]` -- a list of blanks is a
    # truthy list -- so the filter below removed every entry and the
    # function returned NOTHING. A verifier that emitted empty strings
    # silently suppressed the catalogue that was there to cover for it,
    # which is the exact failure this module exists to prevent.
    supplied = [str(r).strip() for r in (existing or []) if str(r).strip()]
    if supplied:
        return list(dict.fromkeys(supplied))

    return list(dict.fromkeys(explain(code) for code in (codes or [])))


def known(code: str) -> bool:
    """Whether a code has a written explanation rather than a derived one."""
    return str(code or "").upper() in CATALOGUE


__all__ = ["CATALOGUE", "DISTINCT_OUTCOMES", "derive", "explain",
           "explain_all", "known"]
