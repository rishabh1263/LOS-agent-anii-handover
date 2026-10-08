"""
THE FAQ (MASTER SPEC section 7; config app/config/faq.yaml, flag COPILOT_FAQ).

Context-aware: the catalogue's items whose conditions hold for THIS conversation (no case / a case open, a
co-applicant, KYC failed, documents pending, ready, stuck), ordered by priority with the boosts that apply now,
rendered as a short categorised list of ask: links (the reply contract, section 8). Facts come from the case
itself (the same row status as the case list), never from the message.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

FLAG = "COPILOT_FAQ"
_PATH = Path(__file__).resolve().parents[4] / "config" / "faq.yaml"
_CFG: dict[str, Any] = {"mtime": None, "data": {}}
_LOCK = threading.Lock()


def cfg() -> dict[str, Any]:
    try:
        mtime = _PATH.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        if _CFG["mtime"] != mtime:
            _CFG["data"] = yaml.safe_load(_PATH.read_text(encoding="utf-8")) or {}
            _CFG["mtime"] = mtime
        return _CFG["data"]


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(cfg().get("enabled", False))


def _norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", re.sub(r"[^\w\sऀ-ॿ'-]", " ", str(text or "").lower())).strip() + " "


def asks_for_faq(text: str) -> bool:
    """Only the FAQ words (and fillers): "help", "kya pooch sakta hoon" -- never "help me upload the PAN"."""
    said = _norm(text)
    for phrase in sorted(cfg().get("ask_phrases") or [], key=len, reverse=True):
        if _norm(phrase) in said:
            said = said.replace(_norm(phrase), " ")
    rest = [w for w in said.split() if w not in {"please", "plz", "me", "mujhe", "bhai", "batao", "show", "the", "a"}]
    return said.strip() != _norm(text).strip() and not rest


def facts(case_id: str | None) -> set[str]:
    """The conditions that hold now (faq.yaml `when` / `boost` names)."""
    if not case_id:
        return {"no_case"}
    held = {"case_open"}
    try:
        from app.agents.applicant.copilot.capabilities import case_list
        from app.store import get_repository

        application = get_repository().get_application(case_id)
        if application is not None:
            row = case_list._row(application)
            kind = row.get("status_kind")
            held |= {"kyc_failed"} if kind == "KYC" or _kyc_open(case_id) else set()
            held |= {"documents_pending"} if kind == "DOCS" else set()
            held |= {"ready"} if kind == "READY" else {"not_ready"}
            held |= {"stuck"} if row.get("over_target") else set()
            if getattr(application, "co_applicant_id", None):
                held.add("has_co_applicant")
            else:
                try:
                    from app.agents.los import co_applicants

                    if co_applicants.list_for_case(case_id):
                        held.add("has_co_applicant")
                except Exception:  # noqa: BLE001 - no co-applicant table: none
                    pass
    except Exception:  # noqa: BLE001 - facts unknown: only the always-true conditions
        held.add("not_ready")
    return held


def _kyc_open(case_id: str) -> bool:
    """A party whose recorded KYC is failed / in review (the same rule as the KYC A / B verdict)."""
    try:
        from app.agents.applicant.copilot.answering import kyc_table

        return any(p["rows"] or p["status"] for p in kyc_table.build(case_id)["parties"]
                   if not kyc_table._passed(p["status"]))
    except Exception:  # noqa: BLE001
        return False


def items(case_id: str | None, lang: str, limit: int | None = None) -> list[dict[str, Any]]:
    """The items that apply now, highest priority first: {id, category, question, send}."""
    from app.agents.applicant.copilot.answering import language_lock

    held = facts(case_id)
    boost = int(cfg().get("boost_by", 50))
    chosen = []
    for item in cfg().get("items") or []:
        if not set(item.get("when") or []) <= held:
            continue
        score = int(item.get("priority", 0)) + (boost if set(item.get("boost") or []) & held else 0)
        question = language_lock.pick(item.get("q") or item.get("id"), lang)
        send = language_lock.pick(item["send"], lang) if item.get("send") else question
        chosen.append((score, {"id": item.get("id"), "category": item.get("category") or "general",
                               "question": str(question), "send": str(send)}))
    chosen.sort(key=lambda s: -s[0])
    out = [c for _, c in chosen]
    return out[:limit] if limit else out


def render(found: list[dict[str, Any]], lang: str, heading_key: str = "heading", seed: int = 0) -> str:
    """A short categorised list: "**Documents:** [Q](ask:...) · [Q](ask:...)" -- categories in catalogue order."""
    from app.agents.applicant.copilot.answering import contract, language_lock

    if not found:
        return ""
    heading = (cfg().get("labels") or {}).get(heading_key) or ""
    heading = language_lock.pick(heading, lang) if isinstance(heading, dict) else heading
    if isinstance(heading, list):
        heading = heading[seed % len(heading)] if heading else ""
    names = cfg().get("categories") or {}
    lines = [str(heading)] if heading else []
    for category in names:
        group = [f for f in found if f["category"] == category]
        if group:
            label = language_lock.pick(names[category], lang)
            links = " · ".join(_link(f, contract) for f in group)
            lines.append(f"- **{label}:** {links}")
    return "\n".join(lines)


def _link(item: dict[str, Any], contract) -> str:
    """[Question](ask:<what it sends>) -- the label is the question, the message is `send`."""
    target = contract.ask(item["send"]).split("](", 1)[1]
    return f"[{item['question']}]({target}"


def _say(value: Any, lang: str, seed: int = 0, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    picked = language_lock.pick(value, lang) if isinstance(value, dict) else value
    if isinstance(picked, list):
        picked = picked[seed % len(picked)] if picked else ""
    return str(picked or "").format(**values)


def answer_for(text: str, lang: str) -> str | None:
    """
    MASTER SPEC 15.2 step 2: a configured "how to" / "what is" answer -- numbered steps + its source -- or None.
    The longest matching phrase wins; nothing is generated.
    """
    from app.agents.applicant.copilot.answering import language_lock

    said = _norm(text)
    best: tuple[int, dict[str, Any]] | None = None
    for item in cfg().get("answers") or []:
        for phrase in item.get("match") or []:
            if _norm(phrase) in said and (best is None or len(phrase) > best[0]):
                best = (len(phrase), item)
    if best is None:
        return None
    item = best[1]
    steps = language_lock.pick(item.get("steps"), lang) or []
    steps = [" ".join(str(s).split()) for s in (steps if isinstance(steps, list) else [steps])]
    lines = [f"{n}. {s}" for n, s in enumerate(steps, 1)]
    source = _say(item.get("source"), lang)
    if source:
        lines += ["", _say((cfg().get("answer_labels") or {}).get("source"), lang, source=source)]
    return "\n".join(lines)


def looks_like_faq(text: str) -> bool:
    """A "how do I / what is" question (faq.yaml unknown.cues)."""
    said = _norm(text)
    return any(_norm(c) in said for c in (cfg().get("unknown") or {}).get("cues") or [])


def unknown(lang: str, seed: int = 0) -> str:
    """'I don't know that yet' -- the configured reply when an FAQ-style question has no answer and no data."""
    return _say((cfg().get("unknown") or {}).get("text"), lang, seed)


def apply_unknown(published: Any, message: str) -> Any:
    """
    Both endpoints, after the pipeline: an FAQ-style question it could not answer (OUT_OF_SCOPE / UNKNOWN) gets
    "I don't know that yet" -- never a guess, never a refusal-sounding line.
    """
    from app.agents.applicant.copilot.answering import language_lock

    if not enabled() or not isinstance(published, dict) or not message or not looks_like_faq(message):
        return published
    if str(published.get("intent") or "").upper() not in ("OUT_OF_SCOPE", "UNKNOWN"):
        return published
    published["answer"] = unknown(language_lock.current() or "en", len(message))
    published.pop("answer_markdown", None)
    published["suggested_questions"] = []
    return published


__all__ = ["FLAG", "answer_for", "asks_for_faq", "cfg", "enabled", "facts", "items", "looks_like_faq", "render",
           "unknown"]
