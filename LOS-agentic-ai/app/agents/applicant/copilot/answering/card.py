"""
THE CLEAN CARD (owner, 2026-10-13: "Swiggy / Zomato jaisa ... response clean, readable").

The last step before a reply is published (contract.publish). Deterministic -- it never adds a fact and never asks a
model; it only makes the same facts easier to read:

  * fixed status icons for the engine's own status words (config `card.icons`): **verified** -> ✅ verified,
    **pending** -> ⏳ pending, **rejected** -> ❌ rejected ... -- set by code, so an icon can never be wrong;
  * one "👉 Next:" line ("**Next step:** The next step is to upload X" -> "👉 **Next:** Upload X");
  * the engine's housekeeping lines a reader does not need (config `card.hide`, regexes: "Its issuing authority has
    not been independently confirmed.", the name-match preamble on a pending list) are dropped;
  * a long table answer (config `card.summary_only_intents`, e.g. the KYC tables) shows its summary only, unless the
    officer asked for the detail (config `card.detail_words`) -- a link asks for it;
  * blank lines collapsed, the first line bold when it is a short sentence.
Off: config `card.enabled` false or env COPILOT_CARD=false.
"""
from __future__ import annotations

import os
import re
from typing import Any


def cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.answering import contract

    return contract.cfg().get("card") or {}


def enabled() -> bool:
    flag = (os.getenv("COPILOT_CARD") or "").strip().lower()
    if flag:
        return flag in {"1", "true", "yes", "on"}
    return bool(cfg().get("enabled", False))


def _split_chrome(md: str) -> tuple[str, str]:
    """(the answer, the trailing buttons / suggestion links) -- the links are never touched."""
    lines = md.rstrip().split("\n")
    cut = len(lines)
    while cut > 0 and (not lines[cut - 1].strip() or re.search(r"\]\((action|ask):", lines[cut - 1])
                       or lines[cut - 1].strip().endswith(":") and cut < len(lines)):
        cut -= 1
    return "\n".join(lines[:cut]).rstrip(), "\n".join(lines[cut:]).strip()


def _summary_only(body: str, intent: str, question: str) -> tuple[str, bool]:
    spec = cfg()
    if intent not in {str(i) for i in spec.get("summary_only_intents") or []}:
        return body, False
    if any(re.search(p, question or "", re.I) for p in spec.get("detail_words") or []):
        return body, False
    lines = body.split("\n")
    first_table = next((i for i, line in enumerate(lines) if line.lstrip().startswith("|")), None)
    if first_table is None:
        return body, False
    # the summary is everything above the first table, minus a section heading left hanging over it
    head = lines[:first_table]
    while head and (not head[-1].strip() or re.fullmatch(r"\s*(\*\*[^*]+\*\*|\*[^*]+\*)\s*", head[-1])):
        head.pop()
    return "\n".join(head).rstrip(), True


def apply(md: str, reply: dict[str, Any] | None = None) -> str:
    """The published markdown as a clean card; unchanged when off or empty."""
    if not str(md or "").strip():
        return md
    reply = reply or {}
    spec = cfg()
    if not enabled():
        if not spec.get("summary_only_always"):
            return md
        # CARD OFF, LONG TABLES STILL SUMMARISED (config card.summary_only_always; 2026-10-24: "is kyc done" and "did
        # the name match" showed two whole KYC tables): only the summary trim and its details link, nothing else
        body, chrome = _split_chrome(md)
        body, trimmed = _summary_only(body, str(reply.get("intent") or ""), str(reply.get("_question") or ""))
        if not trimmed:
            return md
        if spec.get("detail_link"):
            body += "\n\n" + str(spec["detail_link"])
        return body + (f"\n\n{chrome}" if chrome else "")
    body, chrome = _split_chrome(md)
    for pattern in spec.get("hide") or []:
        body = re.sub(pattern, "", body, flags=re.I | re.M)
    body, trimmed = _summary_only(body, str(reply.get("intent") or ""), str(reply.get("_question") or ""))
    for word, icon in (spec.get("icons") or {}).items():
        # only the engine's BOLD status word gets an icon (never a word inside a sentence about something else)
        body = re.sub(rf"(?<![✅⏳❌⚠️] )\*\*{re.escape(word)}\*\*", f"{icon} **{word.lower()}**", body, flags=re.I)
    body = re.sub(r"\*\*Next step:\*\*\s*(?:The next step is to\s+|Agla step:\s*)?", "👉 **Next:** ", body)
    body = re.sub(r"👉 \*\*Next:\*\* ([a-z])", lambda m: "👉 **Next:** " + m.group(1).upper(), body)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    lines = body.split("\n")
    if lines and 0 < len(lines[0]) <= int(spec.get("bold_first_line_max", 90)) and not re.match(
            r"^\s*([-*•|#>]|\d+\.|\*\*|👉)", lines[0]) and "](" not in lines[0] and "**" not in lines[0]:
        lines[0] = f"**{lines[0].strip()}**"
    body = "\n".join(lines)
    if trimmed and spec.get("detail_link"):
        body += "\n\n" + str(spec["detail_link"])
    return body + (f"\n\n{chrome}" if chrome else "")


__all__ = ["apply", "enabled"]
