"""
THE ENTITY FRAME (2026-10-14, owner: "nlu ka 1") -- what ONE officer message names, read once, deterministically, on CPU:

    documents   every document it names, in the order typed ("is pan verified? and bank statement?" -> PAN, BANK_STATEMENT)
    lacks       documents the customer DOES NOT HAVE ("uske paas DL nahi hai", "he doesn't have a passport")
    status_not  documents whose STATUS is negated ("signature not uploaded?") -- a status question, not a lack
    party       CO_APPLICANT when a co-applicant is named, else None
    case_ids    case / applicant ids typed in the message
    stage       a loan stage it names (FOS, CPA, CREDIT ...)
    field       one recorded application field it asks for (loan amount, tenure, product ...)

The vocabulary is the configuration's own (documents.yaml aliases, the product checklists' slots, conversation_general
document_names, stage_sequence, the profile reader's fields); typos are corrected first (normalize.correct_typos). The
only words written for this module are the negation cues -- grammar, not meaning -- in conversation_general.yaml
`entities`. It READS nothing and decides nothing: layers use the frame (the Qwen agent sees it as one line).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field as _field
from functools import lru_cache
from typing import Any


@dataclass
class Frame:
    documents: list[str] = _field(default_factory=list)
    lacks: list[str] = _field(default_factory=list)
    status_not: list[str] = _field(default_factory=list)
    party: str | None = None
    case_ids: list[str] = _field(default_factory=list)
    stage: str | None = None
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v}

    def line(self) -> str:
        """One compact line for the model ("documents: PAN, BANK_STATEMENT; lacks: DRIVING_LICENCE"), "" if empty."""
        parts = []
        for key, value in self.as_dict().items():
            parts.append(f"{key}: {', '.join(value) if isinstance(value, list) else value}")
        return "; ".join(parts)


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import general

    return general.cfg().get("entities") or {}


def _words(text: str) -> str:
    return " " + re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", str(text or "").lower())).strip() + " "


@lru_cache(maxsize=2)
def _aliases(_key: int = 0) -> tuple[tuple[str, str], ...]:
    """(alias, DOCUMENT_CODE), longest alias first -- from the configuration only."""
    from app.agents.applicant import config
    from app.agents.applicant.copilot.capabilities import general
    from app.agents.applicant.copilot.semantics import meaning

    pairs: set[tuple[str, str]] = set()
    docs = meaning._load("documents.yaml").get("documents") or {}
    for code, spec in (docs.items() if isinstance(docs, dict) else []):
        for alias in [*((spec or {}).get("aliases") or []), str(code).replace("_", " ")]:
            pairs.add((str(alias).lower().strip(), str(code).upper()))
    for product in config.products() or []:
        for entry in config.checklist_for(product) or []:
            slot = str(entry.get("slot") or "")
            if slot:
                pairs.add((slot.replace("_", " ").lower(), slot.upper()))
    for code, aliases in ((general.cfg().get("phrases") or {}).get("document_names") or {}).items():
        for alias in aliases or []:
            pairs.add((str(alias).lower().strip(), str(code).upper()))
    return tuple(sorted((p for p in pairs if p[0]), key=lambda p: len(p[0]), reverse=True))


def _spans(said: str) -> list[tuple[int, int, str]]:
    """(start, end, CODE) of every document named, longest alias first, a matched span used up; in typed order."""
    taken = [False] * len(said)
    out = []
    for alias, code in _aliases():
        for m in re.finditer(rf"(?<=\s){re.escape(alias)}(?=\s)", said):
            if any(taken[m.start():m.end()]):
                continue
            for i in range(m.start(), m.end()):
                taken[i] = True
            out.append((m.start(), m.end(), code))
    return sorted(out)


def _negations(said: str, spans: list[tuple[int, int, str]]) -> tuple[list[str], list[str]]:
    """(lacks, status_not): a negation cue within a few words BEFORE the document ("doesn't have a DL", "without
    passport") or AFTER it ("DL nahi hai"); followed by a status word it negates the STATUS, not the possession."""
    spec = _cfg()
    cues = sorted({str(c).lower() for c in spec.get("negation_cues") or []}, key=len, reverse=True)
    status = {str(w).lower() for w in spec.get("status_words") or []}
    window = int(spec.get("window_words", 3))
    if not cues:
        return [], []
    cue_re = re.compile(r"(?<=\s)(" + "|".join(re.escape(c) for c in cues) + r")(?=\s)")
    lacks: list[str] = []
    status_not: list[str] = []
    for start, end, code in spans:
        before = said[:start].split()[-window:]
        after = said[end:].split()[:window]
        near_before = " " + " ".join(before) + " "
        near_after = " " + " ".join(after) + " "
        hit_after = cue_re.search(near_after)
        hit_before = cue_re.search(near_before)
        if not (hit_after or hit_before):
            continue
        # the word right after the cue: a status word -> the STATUS is negated ("not uploaded", "nahi hua verify")
        tail = (near_after[hit_after.end():] if hit_after else near_before[hit_before.end():] + said[start:end]
                + near_after).split()
        next_word = tail[0] if tail else ""
        if next_word in status or any(w in status for w in after[:2]):
            status_not.append(code)
        else:
            lacks.append(code)
    return list(dict.fromkeys(lacks)), list(dict.fromkeys(status_not))


def _stage(said: str) -> str | None:
    from app.agents.applicant.copilot.capabilities import general

    for row in general.cfg().get("stage_sequence") or []:
        code = str((row or {}).get("code") or "")
        if code and re.search(rf"(?<=\s){re.escape(code.lower())}(?=\s)", said):
            return code
    return None


def _field_asked(message: str) -> str | None:
    try:
        from app.agents.applicant.copilot.answering import profile

        asked = profile.detect(message)
    except Exception:  # noqa: BLE001
        return None
    name = str(getattr(asked, "field", "") or "")
    return name if name and name != getattr(profile, "ALL", "__all__") else None


def extract(message: str) -> Frame:
    """The frame of one message (typos corrected first). Never raises: an unreadable message is an empty frame."""
    try:
        from app.agents.applicant import normalize
        from app.agents.applicant.copilot.capabilities import workspace
        from app.agents.applicant.copilot.semantics import meaning

        text = normalize.correct_typos(message).text
        said = _words(text)
        spans = _spans(said)
        lacks, status_not = _negations(said, spans)
        ids = [m.group(0).upper() for p in workspace._ID.values() for m in p.finditer(text)]
        documents = list(dict.fromkeys(code for _, _, code in spans))
        # a FIELD only when no document is named: "address ke liye" next to a DL is the address PROOF, not the
        # applicant's recorded address
        return Frame(documents=documents, lacks=lacks, status_not=status_not, party=meaning.party_in(text),
                     case_ids=list(dict.fromkeys(ids)), stage=_stage(said),
                     field=None if documents else _field_asked(text))
    except Exception:  # noqa: BLE001 - the frame is a convenience; the message goes on as typed
        return Frame()


__all__ = ["Frame", "extract"]
