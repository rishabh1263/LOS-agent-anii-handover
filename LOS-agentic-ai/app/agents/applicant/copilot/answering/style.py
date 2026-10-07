"""
RESPONSE STYLE + THE ABBREVIATION RULE (Phase 3 step 6e; COPILOT_RESPONSE_STYLE, default off).

CHATBOT_SPEC sections 1-2, applied to a finished answer:
  * the first line starts with the status emoji of the spec's emoji map (by intent);
  * an answer that ends with neither a 👉 next step nor a question gets ONE 👉 next step
    (by intent), never two;
  * the FIRST mention of a glossary abbreviation in a session is expanded
    ("**KYC (Know Your Customer)**") and remembered; later mentions stay short;
    a PENDING term (CPA, BOPS, HOPS, RCU, JEV) is never expanded;
  * "X kya hai?" / "X ka full form?" is answered from the glossary: full form + one line
    in the user's language (a PENDING term: the meaning only).

EVERYTHING IS CONFIG: the emoji map, next steps and texts are applicant_agent.yaml
`chatbot.response_style`; the terms, meanings and question shapes app/config/glossary.yaml.
Facts are never changed: only an emoji, a bold full form and one closing line are added.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

FLAG = "COPILOT_RESPONSE_STYLE"
GLOSSARY = Path(__file__).resolve().parents[4] / "config" / "glossary.yaml"


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("response_style") or {}


@lru_cache(maxsize=1)
def glossary() -> dict[str, Any]:
    import yaml

    return yaml.safe_load(GLOSSARY.read_text(encoding="utf-8")) or {}


def terms() -> dict[str, dict[str, Any]]:
    return {str(k).upper(): dict(v or {}) for k, v in (glossary().get("terms") or {}).items()}


def _lang(language: str | None) -> str:
    code = str(language or "en")
    return "hi-Latn" if code.startswith("hi") or code.startswith("mr") else "en"


# --------------------------------------------------------------------------
# "X kya hai?"
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(text or "").lower())).strip()


def defined_term(message: str) -> str | None:
    """The glossary term a definition question asks about ("KYC kya hai?"), else None."""
    said = _norm(message)
    for term in terms():
        for shape in glossary().get("questions") or []:
            if said == _norm(shape.replace("{term}", term)):
                return term
    return None


def definition(term: str, language: str | None) -> str:
    entry = terms()[term]
    meaning = (entry.get("meaning") or {}).get(_lang(language)) or (entry.get("meaning") or {}).get("en", "")
    shown = f"{term} ({entry['full_form']})" if entry.get("status") == "APPROVED" and entry.get("full_form") else term
    return f"ℹ️ **{shown}**: {meaning}"


# --------------------------------------------------------------------------
# the abbreviation rule
# --------------------------------------------------------------------------

def expand_first_mentions(text: str, explained: set[str]) -> tuple[str, set[str]]:
    """Each APPROVED term's first mention in the session -> "**ABBR (Full Form)**". Returns (text, newly explained)."""
    newly: set[str] = set()
    for term, entry in terms().items():
        if term in explained or entry.get("status") != "APPROVED" or not entry.get("full_form"):
            continue
        # a whole word, upper case as written, not part of an id ("CASE-..."), not already expanded
        pattern = re.compile(rf"(?<![\w-])(\*\*)?{re.escape(term)}(\*\*)?(?![\w-])(?!\s*\()")
        found = pattern.search(text)
        if found:
            text = text[:found.start()] + f"**{term} ({entry['full_form']})**" + text[found.end():]
            newly.add(term)
    return text, newly


# --------------------------------------------------------------------------
# the response format
# --------------------------------------------------------------------------

_EMOJI = re.compile(r"^\s*(✅|❌|⏳|⚠️|📄|📤|👤|👥|💰|📍|👉|ℹ️|🙏|🔓|🔒|📂|👋|🙂)")


def format_answer(answer: str, intent: str | None, status: str | None = None) -> str:
    cfg = _cfg()
    text = str(answer or "").strip()
    if not text:
        return text
    emoji = (cfg.get("status_emoji") or {}).get(str(status or "").upper()) \
        or (cfg.get("intent_emoji") or {}).get(str(intent or "").upper())
    if emoji and not _EMOJI.match(text) and not text.startswith("📍 CASE"):
        text = f"{emoji} {text}"
    last = text.rstrip().splitlines()[-1] if text.strip() else ""
    if "👉" not in text and not last.rstrip().endswith("?"):
        step = (cfg.get("next_step") or {}).get(str(intent or "").upper())
        if step:
            text = f"{text}\n\n👉 {step}"
    return text


def apply(published: dict[str, Any], *, explained: set[str], language: str | None = None) -> tuple[dict[str, Any], set[str]]:
    """The styled answer + the terms explained in it (the caller keeps them in session state)."""
    answer = str(published.get("answer") or "")
    if not answer.strip():
        return published, set()
    status = (published.get("readiness") or {}).get("status") if isinstance(published.get("readiness"), dict) else None
    styled = format_answer(answer, published.get("intent"), status)
    styled, newly = expand_first_mentions(styled, explained)
    published["answer"] = styled
    published["response_style"] = {"applied": True, "expanded": sorted(newly)}
    return published, newly


__all__ = ["FLAG", "apply", "defined_term", "definition", "enabled", "expand_first_mentions", "format_answer",
           "glossary", "terms"]
