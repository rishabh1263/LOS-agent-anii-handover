"""
Who a document belongs to.

THE PROBLEM THIS SOLVES. One case can carry two people: a primary applicant
and a co-applicant. Before this, the flow had a single `applicant_id` and
every document was implicitly theirs. Adding a second person without a
party model is how a co-applicant's PAN ends up on the primary applicant's
file — and a KYC comparison then reports a name mismatch between two
documents that were never supposed to describe the same person.

THE RULE, and everything here exists to enforce it:

    ONE CASE. TWO PARTIES. SEPARATE IDENTITY AND DOCUMENT NAMESPACES.

        CASE-001
          ├── APP-001    PRIMARY_APPLICANT
          └── COAPP-001  CO_APPLICANT

    Both belong to the same case. Neither can see or satisfy the other's
    documents, and no document can be attributed to both.

PARTY_ID IS THE PUBLIC ID, DELIBERATELY. A second opaque identifier
alongside `applicant_id` would be one more thing to correlate in a log and
one more place for the two to drift apart. The primary applicant's
`party_id` IS their `applicant_id`; the co-applicant's IS their
`co_applicant_id`. What the party model adds is the ROLE, and the
guarantee that the two namespaces never merge.

BACKWARD COMPATIBILITY. A request with no co-applicant produces exactly one
party, with role PRIMARY_APPLICANT and `party_id == applicant_id`. Every
existing caller keeps working and every existing document keeps its owner.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PartyRole(str, Enum):
    """The role a person plays on one case."""

    PRIMARY_APPLICANT = "PRIMARY_APPLICANT"
    CO_APPLICANT = "CO_APPLICANT"


#: The public request field that carries each role's identifier.
PUBLIC_ID_FIELD = {
    PartyRole.PRIMARY_APPLICANT: "applicant_id",
    PartyRole.CO_APPLICANT: "co_applicant_id",
}

#: Identifier prefixes, so a party id is recognisable in a log line.
_PREFIX = {
    PartyRole.PRIMARY_APPLICANT: "APP",
    PartyRole.CO_APPLICANT: "COAPP",
}

#: An identifier may only contain characters that survive being embedded in
#: a composite document key and a URL without changing meaning.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class PartyError(ValueError):
    """The parties on this request cannot be resolved as described."""


@dataclass(frozen=True)
class Party:
    """One person on one case."""

    party_id: str
    party_role: PartyRole
    #: The case both parties share.
    case_id: str

    @property
    def is_primary(self) -> bool:
        return self.party_role is PartyRole.PRIMARY_APPLICANT

    @property
    def public_field(self) -> str:
        """The request field this party's id arrived in."""
        return PUBLIC_ID_FIELD[self.party_role]

    def public(self) -> dict[str, str]:
        """What a response says about this party's ownership of a document."""
        return {"party_id": self.party_id, "party_role": self.party_role.value}


def _generated(role: PartyRole) -> str:
    return f"{_PREFIX[role]}-{uuid.uuid4().hex[:12].upper()}"


def _validated(value: str, role: PartyRole) -> str:
    identifier = str(value or "").strip()
    if not _SAFE_ID.match(identifier):
        raise PartyError(
            f"{PUBLIC_ID_FIELD[role]} must be 1-128 characters of letters, "
            f"digits, dot, colon, underscore or hyphen."
        )
    return identifier


def resolve(
    *,
    case_id: str,
    applicant_id: str | None = None,
    co_applicant_id: str | None = None,
    require_co_applicant: bool = False,
) -> tuple[Party, Party | None]:
    """
    The parties on this request.

    Returns (primary, co_applicant | None). The primary always exists — a
    case without one is not a case — and an identifier is generated when
    the caller did not supply one, exactly as the flow already did for
    `case_id`.

    REFUSES A SHARED IDENTIFIER. Two parties with the same id are one
    party wearing two hats: their documents would land in the same
    namespace, their checklists would satisfy each other, and a KYC
    comparison would compare a person against themselves and always agree.
    That is a silent wrong answer, so it is rejected loudly instead.
    """
    case = str(case_id or "").strip()
    if not case:
        raise PartyError("case_id is required to resolve parties.")

    primary_id = (
        _validated(applicant_id, PartyRole.PRIMARY_APPLICANT)
        if str(applicant_id or "").strip()
        else _generated(PartyRole.PRIMARY_APPLICANT)
    )
    primary = Party(
        party_id=primary_id,
        party_role=PartyRole.PRIMARY_APPLICANT,
        case_id=case,
    )

    supplied = str(co_applicant_id or "").strip()
    if not supplied and not require_co_applicant:
        return primary, None

    co_id = (
        _validated(supplied, PartyRole.CO_APPLICANT)
        if supplied
        else _generated(PartyRole.CO_APPLICANT)
    )

    if co_id == primary.party_id:
        raise PartyError(
            "applicant_id and co_applicant_id must differ: the two parties "
            "on a case cannot share one identity."
        )

    return primary, Party(
        party_id=co_id,
        party_role=PartyRole.CO_APPLICANT,
        case_id=case,
    )


def document_key(case_id: str, party_id: str, source_id: str) -> str:
    """
    The stored identity of one uploaded document.

    PARTY-QUALIFIED, AND THIS IS THE WHOLE POINT. The key used to be
    `case_id:source_id`. Two people on one case both uploading `pan.jpg`
    — which is what happens, because phones name files the same way —
    produced ONE key, so the second upload silently overwrote the first
    and the primary applicant's PAN became the co-applicant's.

    Including the party id means the two are different rows, owned by
    different people, and neither can displace the other.
    """
    return f"{case_id}:{party_id}:{source_id}"


def legacy_document_key(case_id: str, source_id: str) -> str:
    """
    The key this service used before documents carried a party.

    Looked up as a FALLBACK when the party-qualified key finds nothing, so
    a document stored by an earlier build is adopted and updated in place
    rather than duplicated beside its replacement.
    """
    return f"{case_id}:{source_id}"


def owned_by(
    documents: list[dict[str, Any]], party_id: str, *, is_primary: bool,
) -> list[dict[str, Any]]:
    """
    One party's documents. THE canonical ownership filter.

    ONE FUNCTION, BECAUSE THERE WERE TWO AND THEY DISAGREED. Profile
    matching gave an unstamped document to nobody; the response sections
    and KYC gave it to the primary applicant. On a case carrying
    pre-party rows, KYC would compare a document that profile matching
    reported it could not see. Ownership answered in two places is
    ownership answered twice.

    THE RULE:

        stamped document    -> exact `party_id` match, nothing else
        unstamped document  -> the PRIMARY APPLICANT, and only them

    NEVER BY FILENAME OR DOCUMENT TYPE. Both parties routinely upload
    `pan.jpg` and both routinely send a PAN; neither says whose it is.
    The flow stamps every result it processes, so an unstamped document
    can only be a row written before the party model existed -- and back
    then every document on a case belonged to its applicant. Offering
    that adoption to the co-applicant as well would hand one person's
    document to another, which is the whole failure the party model
    exists to prevent.

    `is_primary` is keyword-only and has NO DEFAULT, so a caller cannot
    acquire the legacy adoption by forgetting about it.
    """
    wanted = str(party_id or "").strip()
    if not wanted:
        return []

    owned = []
    for document in documents:
        stamped = str(document.get("party_id") or "").strip()
        if stamped == wanted or (is_primary and not stamped):
            owned.append(document)

    return owned


__all__ = [
    "Party", "PartyError", "PartyRole", "PUBLIC_ID_FIELD", "document_key",
    "legacy_document_key", "owned_by", "resolve",
]
