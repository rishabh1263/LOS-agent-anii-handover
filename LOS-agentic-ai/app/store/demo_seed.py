"""
A synthetic LOS, for demonstrating the Copilot against something real.

SYNTHETIC, AND SAYS SO. Every identifier begins `DEMO-`, every person is
invented, and `is_seeded` keys on that prefix. Nothing here resembles a
real applicant: the names are placeholders, the PANs are format-valid
and deliberately meaningless, and no row carries a real mobile, email or
address. A reader who finds one of these rows in a store should be able
to tell at a glance that it was planted.

IT WRITES THROUGH THE REPOSITORY, NOT THROUGH SQL. The demo therefore
exercises the same write path the pipeline uses, against whatever
backend is configured, and cannot drift from it. A seed that reached
past the abstraction would be a second way to create a case, and the
two would disagree the first time the real one changed.

IDEMPOTENT. `seed()` returns early when the data is already present, so
running it twice is running it once. The findings table upserts on its
own identity index anyway; the early return covers the tables that do
not, and makes re-seeding cheap rather than merely safe.

THE DEMO NEEDS CASE MEMORY ON. The stage is read from the case
timeline, and `stages.resolve()` reads that only when
`LOS_CASE_MEMORY_ENABLED` is true -- Phase 1B gated it deliberately.
With the flag off, the only other source is `ApplicationStatus`, whose
four values are all inside FOS, so every seeded case would report FOS
and six of the seven stages would be invisible. Seeding does not turn
the flag on: silently enabling persistence because a demo was loaded
is exactly the kind of surprise a flag exists to prevent.

WHY THE STAGE LIVES ON EVENTS. `ApplicationStatus` has four values and
all of them are inside FOS -- it cannot express CPA, CREDIT, RCU, BOPS,
HOPS or DISBURSEMENT. The authoritative stage is the most recent
`CaseEvent.stage`, which is exactly what `stages.resolve()` reads first,
so a seeded case reports its stage through the same path a real one
does.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from app.store.models import (
    Applicant,
    Application,
    ApplicationStatus,
    CaseDecision,
    CaseEvent,
    CaseFinding,
    Document,
    DocumentStatus,
    FindingKind,
)

logger = logging.getLogger(__name__)

#: Every seeded identifier starts with this. It is how `is_seeded`
#: recognises the demo and how a human recognises it in a query result.
PREFIX = "DEMO-"

#: Off unless asked for. Seeding a store that a real case lives in is
#: not something that should happen because a service restarted.
ENV_FLAG = "LOS_DEMO_SEED_ENABLED"


def enabled() -> bool:
    return (os.getenv(ENV_FLAG, "false").strip().lower()
            in {"1", "true", "yes", "on"})


@dataclass(frozen=True)
class SeedSummary:
    """What the seed put in, so a caller can assert on it."""

    applicants: int = 0
    cases: int = 0
    documents: int = 0
    findings: int = 0
    decisions: int = 0
    events: int = 0
    already_present: bool = False

    @property
    def wrote_anything(self) -> bool:
        return not self.already_present and self.cases > 0


# ==========================================================================
# THE DATA
#
# Written out rather than generated, so the demo is the same every time
# and a question about DEMO-CASE-004 has one answer today and tomorrow.
# ==========================================================================

#: Four invented people. `SYNTHETIC` in the address is deliberate and
#: is asserted by a test: it must be impossible to mistake this for a
#: real record, including in a log line that shows only the address.
_APPLICANTS: tuple[dict[str, Any], ...] = (
    {"applicant_id": "DEMO-APP-001", "full_name": "ANANYA RAMESH IYER",
     "mobile": "9000000001", "date_of_birth": "1988-04-12",
     "address": "12 SYNTHETIC ROAD, BENGALURU, KARNATAKA 560001"},
    {"applicant_id": "DEMO-APP-002", "full_name": "VIKRAM SINGH CHAUHAN",
     "mobile": "9000000002", "date_of_birth": "1979-11-30",
     "address": "45 SYNTHETIC LANE, PUNE, MAHARASHTRA 411001"},
    {"applicant_id": "DEMO-APP-003", "full_name": "MEERA KRISHNAN NAIR",
     "mobile": "9000000003", "date_of_birth": "1992-07-05",
     "address": "8 SYNTHETIC STREET, KOCHI, KERALA 682001"},
    {"applicant_id": "DEMO-APP-004", "full_name": "RAJESH KUMAR GUPTA",
     "mobile": "9000000004", "date_of_birth": "1985-01-22",
     "address": "77 SYNTHETIC MARG, JAIPUR, RAJASTHAN 302001"},
)

#: Eight cases across all seven stages. DEMO-APP-001 holds three, so an
#: applicant-level question has something to count and a cross-case
#: question has a history to describe.
_CASES: tuple[dict[str, Any], ...] = (
    {"case_id": "DEMO-CASE-001", "applicant_id": "DEMO-APP-001",
     "stage": "FOS", "status": ApplicationStatus.DOCUMENT_COLLECTION,
     "product": "HOME_LOAN", "loan_amount": "4500000"},
    {"case_id": "DEMO-CASE-002", "applicant_id": "DEMO-APP-001",
     "stage": "CPA", "status": ApplicationStatus.READY_FOR_CPA,
     "product": "HOME_LOAN", "loan_amount": "3200000"},
    {"case_id": "DEMO-CASE-003", "applicant_id": "DEMO-APP-001",
     "stage": "DISBURSEMENT", "status": ApplicationStatus.READY_FOR_CPA,
     "product": "PERSONAL_LOAN", "loan_amount": "800000"},
    {"case_id": "DEMO-CASE-004", "applicant_id": "DEMO-APP-002",
     "stage": "CREDIT", "status": ApplicationStatus.READY_FOR_CPA,
     "product": "HOME_LOAN", "loan_amount": "6100000",
     "co_applicant_id": "DEMO-COAPP-004"},
    {"case_id": "DEMO-CASE-005", "applicant_id": "DEMO-APP-002",
     "stage": "RCU", "status": ApplicationStatus.READY_FOR_CPA,
     "product": "LOAN_AGAINST_PROPERTY", "loan_amount": "9000000"},
    {"case_id": "DEMO-CASE-006", "applicant_id": "DEMO-APP-003",
     "stage": "BOPS", "status": ApplicationStatus.READY_FOR_CPA,
     "product": "HOME_LOAN", "loan_amount": "2750000"},
    {"case_id": "DEMO-CASE-007", "applicant_id": "DEMO-APP-003",
     "stage": "HOPS", "status": ApplicationStatus.READY_FOR_CPA,
     "product": "HOME_LOAN", "loan_amount": "5400000"},
    {"case_id": "DEMO-CASE-008", "applicant_id": "DEMO-APP-004",
     "stage": "FOS", "status": ApplicationStatus.BASIC_DOCUMENT_VERIFICATION,
     "product": "PERSONAL_LOAN", "loan_amount": "1200000"},
)

#: The stages a case passed through to reach where it is. A case in RCU
#: did not appear there -- it came through FOS, CPA and CREDIT, and a
#: question about "what happened before this reached Credit" needs that
#: trail to exist.
_LIFECYCLE = ("FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT")

#: case_id -> the documents on it.
_DOCUMENTS: dict[str, tuple[tuple[str, str, str, tuple[str, ...]], ...]] = {
    "DEMO-CASE-001": (
        ("PAN", "pan.jpg", "VERIFIED", ()),
        ("DRIVING_LICENCE", "dl.jpg", "REVIEW", ("LOW_IMAGE_QUALITY",)),
    ),
    "DEMO-CASE-002": (
        ("PAN", "pan.jpg", "VERIFIED", ()),
        ("BANK_STATEMENT", "bank.pdf", "VERIFIED", ()),
        ("SALARY_SLIP", "payslip.pdf", "VERIFIED", ()),
    ),
    "DEMO-CASE-003": (
        ("PAN", "pan.jpg", "VERIFIED", ()),
        ("BANK_STATEMENT", "bank.pdf", "VERIFIED", ()),
    ),
    "DEMO-CASE-004": (
        ("PAN", "pan.jpg", "VERIFIED", ()),
        ("BANK_STATEMENT", "canara.pdf", "VERIFIED", ()),
        ("PAN", "copan.jpg", "VERIFIED", ()),
    ),
    "DEMO-CASE-005": (
        ("PAN", "pan.jpg", "VERIFIED", ()),
        ("SALE_DEED", "deed.pdf", "REVIEW", ("ADDRESS_MISMATCH",)),
    ),
    "DEMO-CASE-006": (
        ("PAN", "pan.jpg", "VERIFIED", ()),
        ("ITR", "itr.pdf", "VERIFIED", ()),
    ),
    "DEMO-CASE-007": (
        ("PAN", "pan.jpg", "VERIFIED", ()),
        ("BANK_STATEMENT", "bank.pdf", "VERIFIED", ()),
    ),
    "DEMO-CASE-008": (
        ("PAN", "pan.jpg", "REJECTED", ("DOCUMENT_TYPE_MISMATCH",)),
    ),
}

#: case_id -> findings. Five kinds across the set, including the three
#: -- FINANCIAL, RISK and RCU -- that had no producer before this.
_FINDINGS: dict[str, tuple[tuple[FindingKind, str, tuple[str, ...], str], ...]] = {
    "DEMO-CASE-001": (
        (FindingKind.VERIFICATION, "REVIEW", ("LOW_IMAGE_QUALITY",), "dl.jpg"),
        (FindingKind.KYC, "REVIEW", ("ADDRESS_SINGLE_SOURCE",), "pan.jpg"),
    ),
    "DEMO-CASE-002": (
        (FindingKind.VERIFICATION, "PASS", (), "pan.jpg"),
        (FindingKind.KYC, "PASS", (), "pan.jpg"),
    ),
    "DEMO-CASE-003": (
        (FindingKind.VERIFICATION, "PASS", (), "pan.jpg"),
        (FindingKind.FINANCIAL, "PASS", (), "bank.pdf"),
    ),
    "DEMO-CASE-004": (
        (FindingKind.KYC, "REVIEW", ("NAME_MISMATCH",), "canara.pdf"),
        (FindingKind.FINANCIAL, "REVIEW", ("INCOME_SINGLE_SOURCE",),
         "canara.pdf"),
    ),
    "DEMO-CASE-005": (
        (FindingKind.RCU, "FAIL", ("ADDRESS_MISMATCH",), "deed.pdf"),
        (FindingKind.RISK, "REVIEW", ("PROFILE_MISMATCH",), "pan.jpg"),
    ),
    "DEMO-CASE-006": (
        (FindingKind.VERIFICATION, "PASS", (), "pan.jpg"),
        (FindingKind.FINANCIAL, "PASS", (), "itr.pdf"),
    ),
    "DEMO-CASE-007": (
        (FindingKind.VERIFICATION, "PASS", (), "pan.jpg"),
    ),
    "DEMO-CASE-008": (
        (FindingKind.VERIFICATION, "FAIL", ("DOCUMENT_TYPE_MISMATCH",),
         "pan.jpg"),
    ),
}

#: case_id -> (decision, next_action, status).
_DECISIONS: dict[str, tuple[str, str, str]] = {
    "DEMO-CASE-001": ("REVIEW", "REQUEST_VALID_DOCUMENT", "PARTIAL"),
    "DEMO-CASE-002": ("PASS", "CONTINUE", "SUCCESS"),
    "DEMO-CASE-003": ("PASS", "CONTINUE", "SUCCESS"),
    "DEMO-CASE-004": ("REVIEW", "MANUAL_REVIEW", "PARTIAL"),
    "DEMO-CASE-005": ("REVIEW", "MANUAL_REVIEW", "PARTIAL"),
    "DEMO-CASE-006": ("PASS", "CONTINUE", "SUCCESS"),
    "DEMO-CASE-007": ("PASS", "CONTINUE", "SUCCESS"),
    "DEMO-CASE-008": ("REVIEW", "REQUEST_CORRECT_DOCUMENT", "PARTIAL"),
}

#: What a stage records on its way through. Plain sentences, because
#: they are what a reviewer reads and -- in a later phase -- what gets
#: indexed for semantic retrieval. No OCR, no payloads, no paths.
_STAGE_SUMMARY = {
    "FOS": "Documents collected and basic verification completed at the "
           "field stage.",
    "CPA": "Case data checked for completeness and handed to credit "
           "processing.",
    "CREDIT": "Income and obligations assessed against the product's "
              "credit norms.",
    "RCU": "Risk Containment Unit sampled the file for document and "
           "profile authenticity.",
    "BOPS": "Back-office operations validated the sanctioned terms and "
            "the document set.",
    "HOPS": "Head-office operations signed off the file for release.",
    "DISBURSEMENT": "Disbursement instruction prepared against the "
                    "sanctioned amount.",
}


def _party(case: dict[str, Any]) -> str:
    return str(case["applicant_id"])


def _stages_upto(stage: str) -> tuple[str, ...]:
    """Every stage a case passed through to reach this one, inclusive."""
    if stage not in _LIFECYCLE:
        return (stage,)
    return _LIFECYCLE[:_LIFECYCLE.index(stage) + 1]


def is_seeded(repository: Any) -> bool:
    """Whether the demo is already in this store."""
    try:
        return repository.get_application(_CASES[0]["case_id"]) is not None
    except Exception as exc:                       # pragma: no cover
        logger.warning("Could not check for demo data: %r", exc)
        return False


def seed(repository: Any, *, force: bool = False) -> SeedSummary:
    """
    Put the synthetic LOS into this store.

    Returns early when it is already there, so calling this on every
    start-up is safe and cheap. `force` re-writes over the top, which
    the upserting tables handle and the rest tolerate because every id
    is deterministic.
    """
    if not force and is_seeded(repository):
        logger.info("Demo data already present; nothing seeded.")
        return SeedSummary(already_present=True)

    for record in _APPLICANTS:
        repository.save_applicant(Applicant(**record))

    documents = findings = decisions = events = 0

    for case in _CASES:
        case_id = str(case["case_id"])
        applicant_id = _party(case)

        repository.save_application(Application(
            case_id=case_id,
            applicant_id=applicant_id,
            status=case["status"],
            product=case.get("product"),
            loan_amount=case.get("loan_amount"),
            co_applicant_id=case.get("co_applicant_id"),
        ))

        documents += _seed_documents(repository, case_id, applicant_id, case)
        findings += _seed_findings(repository, case_id, applicant_id)
        decisions += _seed_decision(repository, case_id)
        events += _seed_events(repository, case_id, applicant_id, case)

    summary = SeedSummary(
        applicants=len(_APPLICANTS), cases=len(_CASES), documents=documents,
        findings=findings, decisions=decisions, events=events,
    )
    logger.info("Seeded synthetic LOS: %s", summary)
    return summary


def _seed_documents(repository, case_id, applicant_id, case) -> int:
    written = 0
    co_applicant_id = case.get("co_applicant_id")

    for index, (doc_type, source_id, state, codes) in enumerate(
            _DOCUMENTS.get(case_id, ())):
        # The third document on the joint case belongs to the second
        # party. Ownership is recorded, never inferred later.
        co_owned = bool(co_applicant_id) and source_id.startswith("co")
        party_id = co_applicant_id if co_owned else applicant_id

        repository.save_document(Document(
            document_id=f"{case_id}:{party_id}:{source_id}",
            case_id=case_id,
            applicant_id=applicant_id,
            document_type=doc_type,
            party_id=party_id,
            party_role="CO_APPLICANT" if co_owned else "PRIMARY_APPLICANT",
            status=DocumentStatus(state),
            source_id=source_id,
            verification_status={"VERIFIED": "PASS", "REVIEW": "REVIEW",
                                 "REJECTED": "FAIL"}[state],
            reason_codes=list(codes),
        ))
        written += 1

    return written


def _seed_findings(repository, case_id, applicant_id) -> int:
    written = 0

    for index, (kind, status, codes, source_id) in enumerate(
            _FINDINGS.get(case_id, ())):
        repository.save_finding(CaseFinding(
            finding_id=f"{case_id}-F{index + 1}",
            case_id=case_id,
            finding_kind=kind,
            party_id=applicant_id,
            status=status,
            reason_codes=list(codes),
            source_id=source_id,
            document_id=f"{case_id}:{applicant_id}:{source_id}",
            # Deterministic, so a re-seed updates the row it wrote
            # before rather than adding a second one beside it.
            content_hash=f"{case_id}-F{index + 1}",
        ))
        written += 1

    return written


def _seed_decision(repository, case_id) -> int:
    recorded = _DECISIONS.get(case_id)
    if not recorded:
        return 0

    decision, next_action, status = recorded
    repository.save_decision(CaseDecision(
        decision_id=f"{case_id}-D1",
        case_id=case_id,
        decision=decision,
        next_action=next_action,
        status=status,
    ))
    return 1


def _seed_events(repository, case_id, applicant_id, case) -> int:
    """
    The trail that brought this case to where it is.

    ORDERED EXPLICITLY. `sequence` is set rather than left to the store
    to assign, so the timeline reads the same on every seed -- and so
    the LAST event carries the case's current stage, which is what
    `stages.resolve()` reads.

    DEMO FIXTURES, NOT A STAGE TRANSITION. These events place a fixture
    case at a stage for demonstration; they write no stage record and no
    stage history, and they are not how a real case moves. A real case
    changes stage only through `app.agents.los.stage_lifecycle`, whose
    stage record `stages.resolve()` reads ahead of this timeline.
    """
    written = 0

    for index, stage in enumerate(_stages_upto(str(case["stage"]))):
        repository.record_event(CaseEvent(
            event_id=f"{case_id}-E{index + 1}",
            case_id=case_id,
            event_type="STAGE_ENTERED",
            party_id=applicant_id,
            stage=stage,
            summary=_STAGE_SUMMARY.get(stage, f"Case entered {stage}."),
            sequence=index + 1,
        ))
        written += 1

    return written


__all__ = ["ENV_FLAG", "PREFIX", "SeedSummary", "enabled", "is_seeded", "seed"]
