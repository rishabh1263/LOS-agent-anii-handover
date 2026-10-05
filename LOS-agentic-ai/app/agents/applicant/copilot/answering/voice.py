"""
WHO THE ASSISTANT IS TALKING TO -- the customer, or the loan agent working the case.

The answers were written to the applicant ("Your PAN number is ..."). The Copilot
is used by LOAN AGENTS, who ask "what is the customer's PAN number?" -- so for the
AGENT audience the customer is spoken ABOUT, not TO:

    Your PAN number is XXXXXX189E.          -> The customer's PAN number is XXXXXX189E.
    You haven't provided your address yet.  -> The customer hasn't provided their address yet.
    Aapka naam Laxmi ... record hai.        -> Customer ka naam Laxmi ... record hai.
    Your next step is to capture ...         -> The next step is to capture ...

ONE GRAMMATICAL LAYER, NOT PER-ANSWER TEMPLATES: second-person possessives and
"you have/haven't/are" become third person; "your application / case / loan"
become "the application / case / loan"; the next step is the agent's own. Words
addressed to the agent ("I can help you with ...") are left alone. Nothing else is
touched -- values, numbers, names and masking are exactly as the answer had them.

    APPLICANT_AGENT_AUDIENCE = agent (default) | customer
"""

from __future__ import annotations

import os
import re

_RULES: list[tuple[re.Pattern[str], str]] = [
    # the agent's own next step / the case itself -- not "the customer's"
    (re.compile(r"\bYour (next step|next action)\b"), r"The \1"),
    (re.compile(r"\byour (next step|next action)\b"), r"the \1"),
    (re.compile(r"\bYour (application|case|loan application|loan|file|request)\b"), r"The \1"),
    (re.compile(r"\byour (application|case|loan application|loan|file|request)\b"), r"the \1"),
    # verbs of the customer
    (re.compile(r"\bYou haven't\b"), "The customer hasn't"), (re.compile(r"\byou haven't\b"), "the customer hasn't"),
    (re.compile(r"\bYou have not\b"), "The customer has not"), (re.compile(r"\byou have not\b"), "the customer has not"),
    (re.compile(r"\bYou've\b"), "The customer has"), (re.compile(r"\byou've\b"), "the customer has"),
    (re.compile(r"\bYou have\b"), "The customer has"),
    # ("You're welcome" is the assistant's courtesy to the agent, not about the customer)
    (re.compile(r"\bYou are\b(?!\s+welcome)"), "The customer is"),
    (re.compile(r"\bYou're\b(?!\s+welcome)"), "The customer is"),
    (re.compile(r"\b(provided|uploaded|declared|submitted|given|captured) your\b"), r"\1 their"),
    # possessives
    (re.compile(r"\bYour\b"), "The customer's"), (re.compile(r"\byour\b"), "the customer's"),
    (re.compile(r"\byours\b"), "the customer's"),
    # Hinglish / Hindi possessives (a greeting such as "Aapka swagat" is the agent's own)
    (re.compile(r"\bAapk([aie])\b(?!\s+swagat)"), r"Customer k\1"),
    (re.compile(r"\baapk([aie])\b(?!\s+swagat)"), r"customer k\1"),
    (re.compile(r"आपक([ाीे])(?!\s+स्वागत)"), r"ग्राहक क\1"),
    (re.compile(r"\bAapne\b"), "Customer ne"), (re.compile(r"\baapne\b"), "customer ne"),
    (re.compile(r"आपने"), "ग्राहक ने"),
    # Marathi: "तुमचा PAN" -> "ग्राहकाचा PAN"; "तुम्ही ... दिलेला नाही" -> "ग्राहकाने ...".
    # "तुमचे स्वागत" is the agent's own, and "तुम्हाला" (to the agent) is left.
    (re.compile(r"तुम्ही तुमच([ाीे])"), r"ग्राहकाने त्यांच\1"),
    (re.compile(r"तुम्ही(?![ा-ॏ])"), "ग्राहकाने"),
    (re.compile(r"तुमच(्या|[ाीे])(?![ा-ॏ])(?!\s+स्वागत)"), r"ग्राहकाच\1"),
]

#: "the customer's application" reads badly where the rules above met twice
_TIDY = [(re.compile(r"\bthe customer's (application|case|loan|file)\b"), r"the \1"),
         (re.compile(r"\bThe customer's (application|case|loan|file)\b"), r"The \1"),
         (re.compile(r"\bcustomer ke application\b"), "application"),
         (re.compile(r"\bCustomer ke application\b"), "Application"),
         # "ग्राहकाचा अर्ज" / "ग्राहकाच्या अर्जात" -> "अर्ज" / "अर्जात", as "the application"
         (re.compile(r"ग्राहकाच(?:्या|[ाीे])\s+((?:अर्ज|केस)\S*)"), r"\1")]


def audience() -> str:
    return (os.getenv("APPLICANT_AGENT_AUDIENCE") or "agent").strip().lower()


def for_audience(text: str, who: str | None = None) -> str:
    """The answer, spoken to the configured audience. Unchanged for the customer."""
    if not isinstance(text, str) or not text or (who or audience()) != "agent":
        return text
    out = text
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    for pattern, repl in _TIDY:
        out = pattern.sub(repl, out)
    return out


__all__ = ["audience", "for_audience"]
