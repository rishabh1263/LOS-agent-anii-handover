"""
What each LOS stage does — as demonstration material, not as policy.

EVERY ENTRY IS MARKED `DEMO PROCESS KNOWLEDGE`, in the text itself and
in the payload. This is not SBFC policy, it has not been approved by
anybody, and it exists so the Copilot has something stage-specific to
retrieve until real knowledge sources are connected. A reader who meets
one of these paragraphs in an answer must be able to tell immediately
that it is illustrative -- which is why the marker is inside the
indexed text and not only in metadata a UI might drop.

IT IS DELIBERATELY GENERIC. The descriptions are what these stage names
mean across retail lending generally: an RCU samples files for
authenticity, a disbursement desk releases funds against a sanction.
Nothing here is specific to one lender's rules, because inventing a
plausible-looking internal rule is the failure mode that matters -- a
reviewer would act on it, and it would be wrong in a way nobody could
see.

THE FOS BOUNDARY IS UNTOUCHED. This module indexes into the PROCESS
KNOWLEDGE collection. `knowledge_answer.STAGE` still reads "FOS" and
the existing FOS retrieval path is not routed through here.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Said in the indexed text, not only in metadata: a UI that drops the
#: payload must not be able to present this as policy.
MARKER = "DEMO PROCESS KNOWLEDGE"

#: What a process chunk is, in the payload.
SOURCE_TYPE = "PROCESS_KNOWLEDGE"
CHUNK_TYPE = "STAGE_GUIDE"


@dataclass(frozen=True)
class StageGuide:
    """One stage, described."""

    stage: str
    purpose: str
    responsibilities: str
    checks: str
    blockers: str
    inputs: str
    outputs: str
    transition: str
    next_actions: str

    def text(self) -> str:
        """
        The guide as one passage, marked.

        Written as prose rather than a key-value dump because it is
        going to be embedded: "the RCU stage samples files for
        authenticity" retrieves for a question about RCU sampling;
        `checks: sampling` does not.
        """
        return (
            f"[{MARKER}] {self.stage} stage. "
            f"Purpose: {self.purpose} "
            f"Responsibilities: {self.responsibilities} "
            f"Typical checks: {self.checks} "
            f"Common blockers: {self.blockers} "
            f"Expected inputs: {self.inputs} "
            f"Expected outputs: {self.outputs} "
            f"Transition conditions: {self.transition} "
            f"Typical next actions: {self.next_actions} "
            f"This description is synthetic demonstration material and "
            f"is not an official lending policy."
        )


GUIDES: tuple[StageGuide, ...] = (
    StageGuide(
        stage="FOS",
        purpose="The field officer meets the applicant, collects documents "
                "and confirms the file is complete enough to hand onward.",
        responsibilities="Capturing applicant details, collecting identity "
                         "and income documents, and checking each upload is "
                         "readable and of the type it claims to be.",
        checks="Document type matches what was requested, images are "
               "legible, identity fields agree across documents, and the "
               "declared profile matches what the documents say.",
        blockers="A document uploaded as the wrong type, an unreadable "
                 "scan, a missing income document, or identity fields that "
                 "disagree between two documents.",
        inputs="The applicant's identity documents, income documents and "
               "the details captured at the meeting.",
        outputs="A verified document set, cross-document KYC findings and "
                "a readiness verdict for handover.",
        transition="The file moves on once the required documents are "
                   "present and verification has been attempted on each.",
        next_actions="Request a correct or clearer document, or hand the "
                     "case to CPA when the set is complete.",
    ),
    StageGuide(
        stage="CPA",
        purpose="Central processing checks that the file is internally "
                "consistent and complete before any credit view is taken.",
        responsibilities="Reconciling captured data against the documents, "
                         "confirming the checklist is satisfied and raising "
                         "discrepancies back to the field.",
        checks="Applicant and co-applicant details match their documents, "
               "the product's required document list is satisfied, and "
               "earlier findings have been addressed or accepted.",
        blockers="An unresolved field finding, a missing mandatory "
                 "document, or a discrepancy between the captured profile "
                 "and the verified documents.",
        inputs="The FOS document set, verification findings and the "
               "captured applicant profile.",
        outputs="A completeness verdict and a clean file prepared for "
                "credit assessment.",
        transition="The file moves to CREDIT once completeness is "
                   "established and open discrepancies are closed.",
        next_actions="Return the case to the field for correction, or "
                     "release it to credit assessment.",
    ),
    StageGuide(
        stage="CREDIT",
        purpose="Credit assesses whether the applicant can service the "
                "loan being requested.",
        responsibilities="Assessing income, existing obligations and the "
                         "requested amount against the product's norms, and "
                         "recording the reasoning behind the view taken.",
        checks="Income evidence is consistent across documents, obligations "
               "are accounted for, and the requested amount sits within the "
               "product's limits.",
        blockers="Income that cannot be evidenced, income figures that "
                 "disagree across documents, or an amount beyond what the "
                 "evidence supports.",
        inputs="The verified document set, income signals from statements "
               "and salary evidence, and the completeness verdict from CPA.",
        outputs="A credit view with its reasons, and any conditions "
                "attached to a positive view.",
        transition="A file may go to RCU for sampling, or onward for "
                   "operational processing once a view is recorded.",
        next_actions="Request further income evidence, decline, or record "
                     "a credit view with conditions.",
    ),
    StageGuide(
        stage="RCU",
        purpose="The Risk Containment Unit samples files to test whether "
                "the documents and the profile are genuine.",
        responsibilities="Sampling files by risk, checking documents for "
                         "signs of tampering, and confirming that the "
                         "profile presented matches independent evidence.",
        checks="Document authenticity, consistency between the declared "
               "profile and the documents, address verification, and "
               "whether anything in the file looks reused or altered.",
        blockers="A document that cannot be authenticated, an address that "
                 "does not match across sources, or a profile detail that "
                 "independent evidence contradicts.",
        inputs="The full file, the credit view, and any earlier findings.",
        outputs="An authenticity finding, with the specific reason recorded "
                "where the file is flagged.",
        transition="A cleared file continues to operations; a flagged file "
                   "returns for investigation before it can proceed.",
        next_actions="Clear the file, request re-verification, or refer it "
                     "for investigation.",
    ),
    StageGuide(
        stage="BOPS",
        purpose="Back-office operations prepare the sanctioned case for "
                "execution.",
        responsibilities="Validating sanctioned terms against the credit "
                         "view, assembling the execution document set and "
                         "confirming nothing outstanding remains.",
        checks="Sanctioned amount and terms match the recorded view, the "
               "document set is complete for execution, and prior "
               "conditions have been satisfied.",
        blockers="An unmet credit condition, a missing execution document, "
                 "or terms that do not match the recorded view.",
        inputs="The credit view with its conditions, the RCU outcome and "
               "the verified document set.",
        outputs="A validated, execution-ready file.",
        transition="The file moves to head-office operations once the "
                   "terms and the document set are validated.",
        next_actions="Resolve an outstanding condition, or pass the file "
                     "for sign-off.",
    ),
    StageGuide(
        stage="HOPS",
        purpose="Head-office operations give the final sign-off before "
                "funds can be released.",
        responsibilities="Independent review of the assembled file, "
                         "confirming every prior stage recorded its "
                         "outcome, and authorising release.",
        checks="Every preceding stage has a recorded outcome, approvals "
               "are present and within authority, and the file carries no "
               "open exception.",
        blockers="A missing stage outcome, an approval outside authority, "
                 "or an exception nobody has closed.",
        inputs="The execution-ready file from back-office operations.",
        outputs="A signed-off file cleared for disbursement.",
        transition="The file moves to disbursement once sign-off is "
                   "recorded.",
        next_actions="Return the file for a missing approval, or authorise "
                     "disbursement.",
    ),
    StageGuide(
        stage="DISBURSEMENT",
        purpose="Funds are released against the sanction, to the account "
                "the file names.",
        responsibilities="Preparing the disbursement instruction, "
                         "confirming the beneficiary account belongs to the "
                         "applicant, and recording what was released.",
        checks="The beneficiary account holder matches the applicant, the "
               "amount matches the sanction, and the sign-off is present.",
        blockers="A beneficiary account whose holder does not match the "
                 "applicant, an amount that differs from the sanction, or "
                 "a missing sign-off.",
        inputs="The signed-off file and the verified beneficiary account "
               "details.",
        outputs="A disbursement record and the case's closing state.",
        transition="The case completes once the release is recorded.",
        next_actions="Correct the beneficiary details, or record the "
                     "release.",
    ),
)


def guides() -> tuple[StageGuide, ...]:
    return GUIDES


def stages_covered() -> set[str]:
    return {guide.stage for guide in GUIDES}


__all__ = ["CHUNK_TYPE", "GUIDES", "MARKER", "SOURCE_TYPE", "StageGuide",
           "guides", "stages_covered"]
