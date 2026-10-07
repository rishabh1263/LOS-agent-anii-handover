"""
"WHAT IS <LENDING TERM>?" IS A KNOWLEDGE QUESTION (Phase 3 step 3, quick win 3).

Flag COPILOT_TERMS_KNOWLEDGE (default off). "CIBIL kya hai?" was routed out of
scope ("bureau information is handled by the Credit process") because every
mention of a bureau term was. Asking what a term MEANS is not asking for a
bureau result: with the flag on, a definition question about a configured term
is answered from the FOS knowledge base (knowledge/fos/*.md).

A question about THIS case ("mera CIBIL score kitna hai?", "what is my credit
score?") is never a definition: any possessive or case word keeps the old route.
The terms are configuration (applicant_agent.yaml `terms_knowledge.terms`).
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

FLAG = "COPILOT_TERMS_KNOWLEDGE"

_DEFAULT_TERMS = ("cibil", "cibil score", "credit score", "credit bureau", "bureau", "credit report", "foir",
                  "emi", "ltv", "loan to value", "kyc", "cpa", "fos", "sanction", "disbursement", "tenure")

#: whose -- any of these makes it a question about a case, not a definition
_POSSESSIVE = re.compile(r"\b(my|mine|our|his|her|their|mera|meri|mere|hamara|hamari|apna|apni|iska|iski|uska|"
                         r"uski|is\s+case|this\s+case|the\s+case|application\s+ka|customer\s+ka)\b", re.IGNORECASE)


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def _terms() -> tuple[str, ...]:
    try:
        from app.agents.applicant import config

        configured = (config.chatbot("terms_knowledge") or {}).get("terms")
    except Exception:  # noqa: BLE001 - no configuration: the built-in list
        configured = None
    return tuple(str(t).lower() for t in (configured or _DEFAULT_TERMS))


@lru_cache(maxsize=1)
def _pattern() -> re.Pattern[str]:
    term = "|".join(re.escape(t).replace(r"\ ", r"\s+") for t in sorted(_terms(), key=len, reverse=True))
    return re.compile(
        rf"^\s*(what\s+is|what's|whats|what\s+are|explain|define|meaning\s+of|tell\s+me\s+about)\s+"
        rf"(a\s+|an\s+|the\s+)?({term})\b[\s?.!]*$"
        # the normaliser's own canonical form: "CIBIL kya hota hai?" -> "what does cibil mean?"
        rf"|^\s*what\s+does\s+(a\s+|an\s+|the\s+)?({term})\s+mean\b[\s?.!]*$"
        rf"|^\s*({term})\s+(kya|kya\s+hai|kya\s+hota\s+hai|kya\s+hoti\s+hai|ka\s+matlab(\s+kya\s+hai)?|"
        rf"matlab|means?|meaning|kay\s+aahe|mhanje\s+kay)\b[\s?.!]*(hai|h)?[\s?.!]*$",
        re.IGNORECASE)


def is_definition(text: str) -> bool:
    """A definition question about a configured lending term, and about nobody's case."""
    text = " ".join(str(text or "").split())
    return bool(text) and not _POSSESSIVE.search(text) and _pattern().search(text) is not None


__all__ = ["FLAG", "enabled", "is_definition"]
