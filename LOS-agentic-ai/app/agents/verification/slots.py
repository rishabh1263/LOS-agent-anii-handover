"""
Checklist slots, resolved to the document classes that satisfy them.

WHY THIS EXISTS. The checklist a field officer is shown lists a slot called
ADDRESS_PROOF. ADDRESS_PROOF is not a document class -- there is no such card,
no extractor and no classifier for it -- it is the NAME OF A REQUIREMENT that
a driving licence, a passport or a voter ID each satisfy. Before this module
the verifier compared the caller's asserted type against the detected class by
string equality, so a licence uploaded against the slot the checklist had just
asked for was refused as the wrong document.

WHAT THIS IS NOT. This does not loosen the type check. A PAN uploaded against
ADDRESS_PROOF still fails: PAN is not in the slot's accepted set. The check
goes from "equals one class" to "is one of the classes this slot accepts", and
for a plain class name the accepted set is that class alone -- identical
behaviour to before.

WHERE THE MAPPING COMES FROM. The checklist configuration, which is already
the one place in this repository that decides what satisfies a slot
(`accepts:` under each slot in app/config/applicant_agent.yaml). Reading it
here rather than restating it keeps a single source of truth: a slot whose
accepted types change in configuration changes here too, with no second list
to forget.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Asserted values that mean "classify it, do not hold me to a type".
WILDCARDS = frozenset({"", "AUTO", "ANY"})


def _slot_map() -> dict[str, frozenset[str]]:
    """
    Slot name -> the document classes that satisfy it, across every product.

    Union rather than per-product: this answers "is this the right KIND of
    document", which is a question about the taxonomy. Whether a particular
    product requires the slot at all is the checklist's decision, made
    separately and later.
    """
    try:
        from app.agents.applicant import config as checklist_config
    except Exception:  # pragma: no cover - configuration is optional
        return {}

    mapping: dict[str, set[str]] = {}
    try:
        products = checklist_config.products()
    except Exception:
        logger.debug("slot map unavailable; falling back to exact class match")
        return {}

    for product in products:
        for entry in checklist_config.checklist_for(product):
            slot = str(entry.get("slot") or "").upper()
            accepts = {str(a).upper() for a in (entry.get("accepts") or [])}
            if not slot or not accepts:
                continue
            # A slot named after the single class that satisfies it carries no
            # information; leaving it out keeps the map to real aliases.
            if accepts == {slot}:
                continue
            mapping.setdefault(slot, set()).update(accepts)

    return {slot: frozenset(accepts) for slot, accepts in mapping.items()}


def acceptable_classes(requested: str | None) -> frozenset[str]:
    """
    The document classes that satisfy `requested`.

    A class name resolves to itself. A slot name resolves to the classes its
    checklist entry accepts. Anything unrecognised resolves to itself, so an
    unknown assertion is still compared -- and still fails -- rather than
    being silently treated as a wildcard.
    """
    wanted = (requested or "").strip().upper().replace("-", "_")
    if wanted in WILDCARDS:
        return frozenset()
    return _slot_map().get(wanted) or frozenset({wanted})


def satisfies(requested: str | None, detected: str | None) -> bool:
    """True when the detected class satisfies what the caller asserted."""
    accepted = acceptable_classes(requested)
    if not accepted:
        return True
    return str(detected or "").strip().upper() in accepted


def is_slot(requested: str | None) -> bool:
    """True when `requested` names a checklist slot rather than a class."""
    wanted = (requested or "").strip().upper().replace("-", "_")
    return wanted in _slot_map()


def known_slots() -> frozenset[str]:
    """Every slot name that may be asserted in place of a document class."""
    return frozenset(_slot_map())


__all__ = ["WILDCARDS", "acceptable_classes", "is_slot", "known_slots",
           "satisfies"]
