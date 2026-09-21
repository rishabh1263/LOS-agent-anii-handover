"""
The published shape of POST /api/v1/los/process.

DOCUMENTATION ONLY. These models are attached to the route through
`responses=`, not `response_model=`, so FastAPI renders them in Swagger and
does NOT use them to serialise, filter or re-validate anything. That is
deliberate: the response is assembled by app/agents/los/response.py, which
decides what leaves the building, and a second model quietly dropping a key
it had not been taught about is exactly the failure this cannot afford.

WHAT IS NOT HERE IS THE POINT. Stage timings, capability names, routing
categories and the KYC pair-comparison matrix are all real, all useful, and
all internal: they describe how this service is built rather than what it
concluded, and a client that started reading them would turn an
implementation detail into a contract. They remain on the internal envelope
and in the logs.

Keep these in step with app/agents/los/response.py. The E2E suite asserts the
documented field set against a real response, so drift fails a test rather
than quietly misleading a reader.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvidenceRef(BaseModel):
    """
    Where a finding came from, as a reference the caller can resolve.

    Anchored on the caller's own source_id -- never an internal or temporary
    file path, and never the capability's wording for how it looked.
    """

    source_id: str = Field(..., examples=["sig.png"])
    locator: str = Field(
        ...,
        description="source_id, optionally with #page=N or #region=x0,y0,x1,y1.",
        examples=["sig.png#region=0,136,640,166", "deed.pdf#page=1"],
    )


class SignatureFinding(BaseModel):
    """The four questions a caller actually asks about a signature."""

    present: bool
    presence: str = Field(..., examples=["PRESENT", "ABSENT"])
    quality: str = Field(..., examples=["SUFFICIENT", "POOR"])
    comparison: str = Field(..., examples=["MATCH", "NOT_COMPARABLE"])
    match_score: float | None = None
    synthetic_risk: str = Field(..., examples=["LOW"])
    manipulation_risk: str = Field(..., examples=["LOW"])


class SpecialistVerdict(BaseModel):
    """
    A specialist capability's verdict on one document.

    The business answer only: what was decided, why, and what it refused to
    claim. Which service ran, how it was called and what it routed on are
    this service's business.
    """

    model_config = ConfigDict(extra="allow")

    decision: str = Field(..., examples=["PASS", "REVIEW", "FAIL", "SKIPPED"])
    reason_codes: list[str] = Field(default_factory=list)

    subtype: str | None = Field(
        None,
        description=(
            "The kind of instrument, where that changes what the document "
            "is worth. A gift deed is not a sale deed."
        ),
        examples=["SALE_DEED", "GIFT_DEED"],
    )
    signature: SignatureFinding | None = None

    ownership_verified: bool | None = Field(
        None, description="Never true: no capability here establishes ownership."
    )
    authenticity_verified: bool | None = Field(
        None, description="Never true: authenticity is not established here."
    )
    business_existence_verified: bool | None = None


#: What the identity verification gate is and is not claiming.
AUTHENTICITY_DESCRIPTION = (
    "Present on identity documents. Always NOT_ESTABLISHED: this service has "
    "no issuer API, government lookup or issuer signature to check against, "
    "so a PASS means the document is structurally valid and internally "
    "consistent -- never that it was issued by the authority it names."
)


class DocumentError(BaseModel):
    code: str = Field(..., examples=["INVALID_DOCUMENT"])
    message: str
    source_id: str | None = None


class ProcessedDocument(BaseModel):
    """One uploaded document, as the client sees it."""

    source_id: str = Field(..., description="The filename as uploaded.")
    party_id: str | None = Field(
        None,
        description=(
            "Which person on the case this document belongs to. Present "
            "whenever parties were supplied. Two people on one case may "
            "upload files with the same name — this is what tells them "
            "apart, and a frontend should group documents by it rather "
            "than by filename or upload order."
        ),
        examples=["APP-3D51FFAC6342"],
    )
    party_role: str | None = Field(
        None,
        description="`PRIMARY_APPLICANT` or `CO_APPLICANT`.",
        examples=["PRIMARY_APPLICANT"],
    )
    type: str = Field(
        ...,
        description="The type this service identified. UNKNOWN when it could not.",
        examples=["PAN", "DRIVING_LICENCE", "ITR", "BANK_STATEMENT"],
    )
    status: str = Field(
        ...,
        description="This document's roll-up.",
        examples=["SUCCESS", "REVIEW", "SKIPPED", "FAILED"],
    )
    verification: str = Field(
        ...,
        description=(
            "The verification verdict. SKIPPED is NOT a pass -- a check that "
            "did not run has established nothing, and extraction is withheld."
        ),
        examples=["PASS", "REVIEW", "FAIL", "SKIPPED"],
    )

    extraction: dict[str, Any] | None = Field(
        None,
        description=(
            "The extracted fields, released ONLY behind a verification PASS "
            "and only while extraction is enabled. Absent otherwise. The keys "
            "depend on the document type.\n\n"
            "Values are JSON-native: money is a **number**, never a "
            "stringified `Decimal`. An `address` is an **object** of "
            "components rather than the printed line, and is omitted "
            "when no component could be identified -- a field whose "
            "value would be the recogniser's read of a region is not a "
            "field worth publishing."
        ),
        examples=[{
            "pan_number": "ABCPV1234K",
            "name": "SUNIL KUMAR VERMA",
            "date_of_birth": "1988-04-12",
        }],
    )
    reason_codes: list[str] | None = Field(
        None, description="Why this verdict. Absent when there is nothing to say."
    )
    verification_score: int | None = Field(
        None, ge=0, le=100,
        description=(
            "How much of what should have been established about this "
            "document was. NOT a risk or credit score: it describes the "
            "document and the checking of it, and no figure on a bank "
            "statement moves it."
        ),
        examples=[91],
    )
    verification_confidence: int | None = Field(
        None, ge=0, le=100,
        description=(
            "How far `verification_score` can be relied on -- the share of "
            "checks that reached a conclusion either way. "
            "A document that plainly fails every check scores 0 with HIGH "
            "confidence. One whose checks could not run scores 0 with LOW "
            "confidence. Those are very different situations and one number "
            "cannot say both."
        ),
        examples=[94],
    )
    reasons: list[str] | None = Field(
        None,
        description=(
            "Plain-language explanation for a REVIEW or FAIL, one per reason "
            "code. Absent on a pass."
        ),
        examples=[["This bank statement needs review because its transaction "
                   "integrity could not be established confidently."]],
    )
    has_extracted_fields: bool | None = Field(
        None,
        description="Whether the verification gate released any fields.",
    )
    authenticity: str | None = Field(
        None,
        description=AUTHENTICITY_DESCRIPTION,
        examples=["NOT_ESTABLISHED"],
    )
    advisories: list[str] | None = Field(
        None,
        description=(
            "Findings a reviewer should see that are NOT grounds to refuse "
            "the document, and did not change the verdict. Deliberately "
            "separate from `reason_codes`, where a code reads as a problem "
            "with the document."
        ),
        examples=[["PAN_NAME_INITIAL_MISMATCH"]],
    )
    specialist: SpecialistVerdict | None = Field(
        None, description="Present only for a specialist-handled document."
    )
    expected_type: str | None = Field(
        None,
        description=(
            "The type the caller asserted for this file through "
            "expected_types, echoed back. Present whenever they asserted "
            "one; absent for AUTO. Read it beside `type`: where the two "
            "differ the document carries DOCUMENT_TYPE_MISMATCH, failed "
            "verification and released no fields."
        ),
        examples=["PAN"],
    )
    hint: str | None = Field(
        None,
        description=(
            "The type the CALLER asserted, echoed when classification was "
            "switched off. Never a type this service found."
        ),
    )
    evidence_refs: list[EvidenceRef] | None = None
    errors: list[DocumentError] | None = None


class KycFieldSource(BaseModel):
    """One document's contribution to a KYC field comparison."""

    source_id: str = Field(..., description="The filename as uploaded.")
    document_type: str = Field(..., examples=["PAN", "DRIVING_LICENCE"])
    value: str | dict[str, str] | float | int | None = Field(
        None,
        description=(
            "What this document carried, as extraction released it. Already "
            "published under documents[].extraction -- never OCR text, "
            "tokens or boxes.\n\n"
            "An ADDRESS arrives as components (`pincode`, `state`, "
            "`city`, and the free-text parts where they are short "
            "enough to be real ones), because the printed line on a "
            "card is read as one region and its unplaced remainder is "
            "recogniser output, not a value. Omitted entirely when "
            "nothing usable could be identified."
        ),
    )
    normalized_value: str | dict[str, str] | float | int | None = Field(
        None,
        description=(
            "What was actually compared. Published beside `value` so a "
            "near-miss can be read correctly: the difference is either in "
            "the documents or in the normalisation, and only showing both "
            "says which.\n\n"
            "**Absent when normalisation changed nothing** — on most rows "
            "it did, and repeating the value said the same thing twice. "
            "Also absent on an ADDRESS, where the canonical components "
            "in `value` are themselves the normalised form."
        ),
    )


class KycFieldResult(BaseModel):
    """
    One comparable identity field, resolved across the uploaded documents.

    The operational view. The pairwise comparison matrix behind it stays
    internal: it grows with the square of the document count and nobody acts
    on it.
    """

    field: str = Field(
        ...,
        examples=["NAME", "DATE_OF_BIRTH", "PAN_NUMBER", "FATHER_NAME",
                  "ADDRESS"],
    )
    party_id: str | None = Field(
        None,
        description=(
            "Whose row this is. Present **only** on the case-level `kyc` "
            "of a two-party case, where both parties contribute a NAME "
            "row and a reviewer must be able to tell them apart. Absent "
            "on a single-applicant response and inside a party's own "
            "`kyc`, where it would say nothing."
        ),
        examples=["APP-1", "COAPP-7F2A11C4D9E0"],
    )
    status: str = Field(
        ...,
        description=(
            "PASS the values agree. PARTIAL they overlap materially but are "
            "not identical. REVIEW the comparison was inconclusive. FAIL they "
            "plainly differ. SKIPPED nothing was compared -- which is NOT a "
            "failure and never becomes one."
        ),
        examples=["PASS", "PARTIAL", "REVIEW", "FAIL", "SKIPPED"],
    )
    match_score: int = Field(
        0, ge=0, le=100,
        description=(
            "How closely the available values match EACH OTHER. 0 on a "
            "SKIPPED field means nothing was compared, not that nothing "
            "matched."
        ),
    )
    confidence: int = Field(
        0, ge=0, le=100,
        description=(
            "How far this result can be relied on -- a DIFFERENT question "
            "from match_score, and never a copy of it. Built from the "
            "comparison method used, how many documents were compared, how "
            "complete the field was across them, the extraction quality "
            "reported upstream, and how near the score sits to a decision "
            "threshold. "
            "Two poorly-read fields that agree exactly score 100 for match "
            "and well under it for confidence. Two cleanly-read fields that "
            "plainly differ score 0 for match and HIGH for confidence -- the "
            "disagreement is real and a reviewer should act on it."
        ),
    )
    reason_code: str = Field(
        "", examples=["EXACT_MATCH", "PARTIAL_ADDRESS_MATCH", "NAME_MISMATCH"]
    )
    reason: str | None = Field(
        None,
        description=(
            "Deterministic. Never model-generated.\n\n"
            "**Absent whenever `reason_code` is present**, which is "
            "almost always: `NAME_MISMATCH` beside \"Name differs "
            "across PAN and Driving Licence.\" is the same fact twice, "
            "and the sentence was the largest thing in a party's KYC "
            "after the sources. Present only on the rare row that "
            "reached a verdict without a code, where the sentence is "
            "the only explanation there is."
        ),
        examples=["Name matches across PAN and Driving Licence."],
    )
    sources: list[KycFieldSource] = Field(default_factory=list)


class KycSummary(BaseModel):
    """
    The KYC verdict.

    Establishes that the documents describe the same person consistently. It
    does NOT establish that any of them is genuine, and it is NOT a credit,
    risk or fraud score.
    """

    status: str = Field(..., examples=["PASS", "REVIEW", "FAIL", "SKIPPED"])
    reason_codes: list[str] = Field(default_factory=list)

    overall_score: int = Field(
        0, ge=0, le=100,
        description=(
            "Weighted mean of the field match scores, over the fields that "
            "were actually compared. Weights are in kyc_policies.yaml. A "
            "skipped field is excluded and the rest renormalised, so an "
            "absent field neither helps nor hurts. NOT a risk score."
        ),
    )
    overall_confidence: int = Field(
        0, ge=0, le=100,
        description="Weighted mean of the field confidences, same basis.",
    )
    fields: list[KycFieldResult] | None = Field(
        None,
        description=(
            "One row per comparable identity field.\n\n"
            "**Absent from the case-level `kyc` on a two-party case**, "
            "where every row is already published under the party it "
            "belongs to and a second copy could not say whose was "
            "whose. Present on a single-applicant case, and always "
            "present inside a party's own `kyc`."
        ),
    )


class CrossDocumentCheck(BaseModel):
    """One named agreement check across the uploaded documents."""

    check: str = Field(..., examples=["NAME", "DOB", "ADDRESS", "PAN", "INCOME"])
    status: str = Field(
        ...,
        description="SKIPPED means there was nothing to compare, not agreement.",
        examples=["PASS", "REVIEW", "FAIL", "SKIPPED"],
    )
    reason_codes: list[str] = Field(default_factory=list)
    sources: list[str] | None = Field(
        None,
        description=(
            "The documents this check compared -- or, where it failed, the "
            "ones that disagree."
        ),
        examples=[["pan.jpg", "itr.pdf"]],
    )
    details: dict[str, str] | None = Field(
        None,
        description=(
            "What each document said for the compared field, by source_id. "
            "Present only where the check did not pass, so a reviewer can "
            "see the disagreement without reopening every file. These are "
            "the same normalised values already published under "
            "documents[].extraction -- never OCR text or candidates."
        ),
        examples=[{"pan.jpg": "MUKESH KUMAR", "dl.jpg": "RISHABH AJIT SINGH"}],
    )


class CrossDocument(BaseModel):
    """
    Agreement between documents.

    An object rather than a list of disagreements, because a bare list could
    not distinguish "every field agreed" from "nothing was comparable" --
    both arrived empty, and they mean opposite things to a reviewer.
    """

    status: str = Field(
        ...,
        description=(
            "Worst of the checks that actually ran, capped by policy: a "
            "failed check that is not marked blocking in kyc_policies.yaml "
            "contributes at most REVIEW. Documents disagreeing routes the "
            "case to a human; it is not treated as fraud. SKIPPED if nothing "
            "was comparable."
        ),
        examples=["PASS", "REVIEW", "FAIL", "SKIPPED"],
    )
    checks: list[CrossDocumentCheck] = Field(default_factory=list)


class ProfileMatchSource(BaseModel):
    """The document a profile field was checked against."""

    source_id: str = Field(..., examples=["pan.jpg"])
    document_type: str | None = Field(None, examples=["PAN"])


class ProfileFieldMatch(BaseModel):
    """One declared field, compared against this party's own documents."""

    field: str = Field(..., examples=["PAN_NUMBER"])
    status: str = Field(
        ...,
        description=(
            "PASS, PARTIAL, FAIL or SKIPPED. **SKIPPED is not a mismatch.** "
            "It means the comparison could not be made -- the value was "
            "never supplied, or no verified document released it."
        ),
        examples=["PASS", "PARTIAL", "FAIL", "SKIPPED"],
    )
    match_score: int = Field(
        ...,
        description=(
            "0-100, how closely the two values agree. 0 on a SKIPPED field "
            "means nothing was compared, not that nothing matched."
        ),
        examples=[100],
    )
    confidence: int = Field(
        ...,
        description=(
            "0-100, how far that answer can be relied on. NOT a rescale of "
            "match_score: a perfect match read off a poor photograph is a "
            "high score at a lower confidence."
        ),
        examples=[94],
    )
    reason_code: str | None = Field(None, examples=["PROFILE_MATCH"])
    reason: str | None = Field(
        None,
        examples=["The PAN on PAN matches the one supplied for this party."],
    )
    source: ProfileMatchSource | None = None


class PartyProfileMatch(BaseModel):
    """
    One party's declared profile against that party's own documents.

    SEPARATE FROM KYC, AND SEPARATE PER PARTY. KYC asks whether the
    documents agree with each other; this asks whether they describe the
    person the application declared. The primary applicant's profile is
    only ever compared with the primary applicant's documents.

    IT DECIDES NOTHING. Every verdict, reason code and score elsewhere in
    this response is what verification found, with or without this block.
    """

    party_id: str = Field(..., examples=["APP-1"])
    party_role: str = Field(..., examples=["PRIMARY_APPLICANT", "CO_APPLICANT"])
    score: int = Field(
        ...,
        description=(
            "0-100 over the fields ACTUALLY COMPARED. A field nobody could "
            "compare contributes nothing, so a gap never reads as a "
            "disagreement -- read it beside fields_compared."
        ),
        examples=[100],
    )
    confidence: int = Field(..., examples=[94])
    fields_expected: int = Field(
        ..., description="Declared for this party.", examples=[3]
    )
    fields_extracted: int = Field(
        ..., description="Released by this party's documents.", examples=[2]
    )
    fields_compared: int = Field(
        ...,
        description="Present on both sides, so actually checked.",
        examples=[2],
    )
    fields: list[ProfileFieldMatch] = Field(default_factory=list)


class PartyVerificationSummary(BaseModel):
    """
    How one party's documents came out, counted.

    DERIVED, NEVER DECIDED. Every number is a tally of verdicts
    verification already reached; nothing here can change an outcome. The
    four buckets always sum to `total_documents`.
    """

    total_documents: int = Field(..., examples=[3])
    passed: int = Field(..., examples=[2])
    review: int = Field(..., examples=[1])
    failed: int = Field(
        ...,
        description=(
            "FAILED and REJECTED together: one means the file could not "
            "be processed and the other that it was processed and "
            "refused, and triage treats both the same. The distinction "
            "survives on each document's own `verification`."
        ),
        examples=[0],
    )
    skipped: int = Field(..., examples=[0])


class PartySection(BaseModel):
    """
    One party's slice of the response: whose, what they sent, how it went.

    A REGROUPING, NOT A SECOND RESULT. `document_ids` names this
    party's entries in the top-level `documents[]`, split by the
    `party_id` stamped on each one -- never by filename or document
    type, because both parties routinely upload `pan.jpg` and both
    routinely send a PAN.

    `primary_applicant` is always present. `co_applicant` appears only
    when the case actually has a second party.
    """

    party_id: str = Field(..., examples=["APP-1"])
    role: str = Field(..., examples=["PRIMARY_APPLICANT", "CO_APPLICANT"])
    status: str | None = Field(
        None,
        description=(
            "**This party's document and KYC state only** -- the same "
            "worst-wins roll-up the case uses, over this party's own "
            "documents and their own KYC.\n\n"
            "It is NOT a decision and there is deliberately no "
            "party-level `next_action`: a party-level CONTINUE beside a "
            "case-level MANUAL_REVIEW would read as permission to "
            "proceed. There is ONE decision on a loan, and `decision` "
            "and `next_action` stay at the top level where they are "
            "true.\n\n"
            "A party who has been declared but has uploaded nothing "
            "reports REVIEW, never SUCCESS -- nothing is known about "
            "them yet."
        ),
        examples=["SUCCESS", "PARTIAL", "REVIEW", "REJECTED", "FAILED"],
    )
    document_ids: list[str] = Field(
        default_factory=list,
        description=(
            "This party's documents, by `source_id` -- the filename as "
            "uploaded.\n\n"
            "**References, not copies.** The full objects are published "
            "once in the top-level `documents[]`, each already carrying "
            "its own `party_id`; repeating them here put every document "
            "in the response twice. Join on `source_id` within a party, "
            "or filter `documents[]` by `party_id` directly."
        ),
        examples=[["pan.jpg", "dl.jpg"]],
    )
    verification_summary: PartyVerificationSummary
    profile_match: PartyProfileMatch | None = Field(
        None,
        description=(
            "**Absent** when this party had no declared or stored profile "
            "to match -- an empty object would read as 'we matched and "
            "found nothing', which is a much stronger claim."
        ),
    )
    kyc: KycSummary | None = Field(
        None,
        description=(
            "**This party's own** cross-document KYC: do THEIR documents "
            "describe one person? Never compared against the other "
            "party's documents -- two people disagreeing is what a joint "
            "application IS, not evidence against it. Absent when this "
            "party released nothing to cross-check."
        ),
    )


class LosProcessResponse(BaseModel):
    """One applicant's documents, processed end to end."""

    request_id: str = Field(..., examples=["los_8604ee9d1f2b4c0a9e7d3f1a2b3c4d5e"])
    applicant_id: str | None = None
    co_applicant_id: str | None = Field(
        None,
        description=(
            "The second party on this case. **Absent entirely** on a "
            "single-applicant case — a null here would read as 'there is "
            "a co-applicant and we do not know who'. Both parties share "
            "the one `case_id`; each document says which of them it "
            "belongs to in `documents[].party_id`."
        ),
        examples=["COAPP-7F2A11C4D9E0"],
    )
    case_id: str = Field(
        ...,
        description="Generated when none is supplied. One applicant may hold several.",
    )
    status: str = Field(
        ...,
        description="Worst-wins across every document and the KYC result.",
        examples=["SUCCESS", "PARTIAL", "REVIEW", "REJECTED", "FAILED"],
    )
    documents: list[ProcessedDocument]
    kyc: KycSummary
    cross_document: CrossDocument
    decision: str = Field(
        ...,
        description=(
            "The deterministic outcome. NOT a credit decision and not an "
            "underwriting verdict: it restates the roll-up under a name a "
            "client can read. PASS requires that something was actually "
            "verified."
        ),
        examples=["PASS", "REVIEW", "REJECT"],
    )
    next_action: str = Field(
        ...,
        description=(
            "What should happen to this application next, derived from "
            "results that are already final. `REQUEST_CORRECT_DOCUMENT` "
            "means the wrong file was sent and "
            "`REQUEST_VALID_DOCUMENT` that the right one could not be "
            "read -- the applicant has to do different things about "
            "those two.\n\n"
            "Case-level and authoritative: there is no party-level "
            "`next_action`, because one beside a case-level "
            "`MANUAL_REVIEW` would read as permission to proceed."
        ),
        examples=["CONTINUE", "MANUAL_REVIEW", "REQUEST_VALID_DOCUMENT",
                  "REQUEST_CORRECT_DOCUMENT"],
    )
    summary: str = Field(..., description="One sentence. Never decides anything.")
    summary_source: str = Field(
        ...,
        description=(
            "Who wrote the summary sentence. Published because a client "
            "showing it to a human needs to know whether a model produced "
            "it; every decision above was already final when it was written."
        ),
        examples=["deterministic", "llm"],
    )
    processing_ms: float = Field(
        ..., description="How long the call took. Stage timings stay internal."
    )
    profile_match: list[PartyProfileMatch] | None = Field(
        None,
        description=(
            "One entry per party whose profile was supplied or held on "
            "file. **Absent entirely** when no profile was available to "
            "match -- this block is additional evidence and never changes "
            "a verdict above."
        ),
    )
    primary_applicant: PartySection | None = Field(
        None,
        description=(
            "The primary applicant's documents, profile match and "
            "verification counts, grouped. The same results as the "
            "top-level fields, not a second computation."
        ),
    )
    co_applicant: PartySection | None = Field(
        None,
        description=(
            "**Absent entirely** on a single-applicant case. Present only "
            "when the request supplied a second party."
        ),
    )
    errors: list[DocumentError] = Field(default_factory=list)


__all__ = [
    "LosProcessResponse", "ProcessedDocument", "SpecialistVerdict",
    "SignatureFinding", "EvidenceRef", "KycSummary", "KycFieldResult",
    "KycFieldSource", "CrossDocument",
    "CrossDocumentCheck", "DocumentError",
    "PartyProfileMatch", "ProfileFieldMatch", "ProfileMatchSource",
    "PartySection", "PartyVerificationSummary",
]
