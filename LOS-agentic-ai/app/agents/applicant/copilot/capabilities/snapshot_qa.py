"""
CASE SNAPSHOT QA + FACT CHECK (FOS plan 6.4 / 6.5 / 6.6 / 11d; flag COPILOT_SNAPSHOT_QA, config
chatbot.snapshot_qa -- ON in dev). One model call at most; CPU friendly.

THE LONG TAIL. A question about the opened case that no intent fits ("salary slip kis company ki hai", "PAN pe DOB
kya hai", "kitne documents verified hain", "kab upload hua") is answered by the model ONLY from a compact, masked
FACT SHEET of that case -- key: value lines built here from the store -- and it must cite the keys it used.

THE FACT CHECK (6.5). Every number, date, ID, status and document name in the generated answer must be found in the
fact sheet, and every cited key must exist. Anything else -> the answer is DISCARDED and the reply says what is not
recorded and where it would come from (`answerability`, 11d). Wrong data is never published: the check is
deliberately strict, so a correct answer that cannot be proven is dropped too.

NEVER: a refusal, a write, another case, a credit decision. Ownership is checked by the caller before this runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)
FLAG = "COPILOT_SNAPSHOT_QA"
_ON = {"1", "true", "yes", "on"}


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("snapshot_qa")


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    return bool(_cfg().get("enabled", False))


def _label(key: str, **values: Any) -> str:
    from app.agents.applicant.copilot.answering import language_lock

    defaults = {"not_recorded": "This is not recorded on the case.",
                "where": "{what}", "sources": "Based on: {keys}."}
    return language_lock.pick((_cfg().get("labels") or {}).get(key, defaults.get(key, key))).format(**values)


# ==========================================================================
# THE FACT SHEET
# ==========================================================================

def _value(x: Any) -> str:
    return str(getattr(x, "value", x) or "")


def _masked(field: str, value: Any) -> str | None:
    from app.security import sensitivity

    if value in (None, "", [], {}):
        return None
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)[:160]
    disclosure = sensitivity.disclosure(field)
    if disclosure == "withhold":
        return None
    text = sensitivity.mask(str(value)) if disclosure == "masked" else str(value)
    return sensitivity.mask_identifiers(text)[:200]


def fact_sheet(case_id: str, repository: Any = None) -> dict[str, str]:
    """key -> masked value, for one case. Only recorded facts; nothing derived beyond counts."""
    from app.agents.applicant.copilot.answering.answer import _explained
    from app.store import get_repository

    repository = repository or get_repository()
    application = repository.get_application(case_id)
    if application is None:
        return {}
    facts: dict[str, str] = {"case.id": case_id}
    for key in ("product", "loan_amount", "status", "employment_type", "tenure_months", "interest_rate_pct",
                "declared_monthly_income", "declared_monthly_obligations", "property_value", "created_at",
                "updated_at"):
        shown = _masked(key, getattr(application, key, None))
        if shown:
            facts[f"application.{key}"] = _value(shown)
    applicant = repository.get_applicant(application.applicant_id)
    for key in ("full_name", "mobile", "email", "date_of_birth", "address"):
        shown = _masked(key, getattr(applicant, key, None)) if applicant else None
        if shown:
            facts[f"applicant.{key}"] = shown
    documents = repository.list_documents(case_id) or []
    types = {}
    counts: dict[str, int] = {}
    for d in documents:
        status = _value(getattr(d, "status", "")).upper()
        if status == "SUPERSEDED":
            continue
        kind = str(d.document_type).upper()
        types[d.document_id] = kind
        party = "co_applicant" if _value(getattr(d, "party_role", "")).upper() == "CO_APPLICANT" else "applicant"
        base = f"document.{party}.{kind.lower()}"
        facts[f"{base}.status"] = status
        reasons = [r for r in (_explained(c) for c in (getattr(d, "reason_codes", None) or [])) if r]
        if reasons:
            facts[f"{base}.reason"] = "; ".join(reasons[:2])
        for stamp in ("uploaded_at", "verified_at", "updated_at"):
            if getattr(d, stamp, None):
                facts[f"{base}.{stamp}"] = str(getattr(d, stamp))[:19]
        counts[status] = counts.get(status, 0) + 1
    for status, n in counts.items():
        facts[f"documents.count.{status.lower()}"] = str(n)
    facts["documents.count.total"] = str(sum(counts.values()))
    try:
        for f in repository.get_current_findings(case_id, kind="EXTRACTION") or []:
            kind = types.get(getattr(f, "document_id", None))
            payload = getattr(f, "payload", None) or {}
            fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
            if not kind or not isinstance(fields, dict):
                continue
            for key, value in list(fields.items())[: int(_cfg().get("max_fields_per_document", 14))]:
                if key in ("signals", "raw_text", "ocr_text", "lines"):
                    continue
                shown = _masked(key, value)
                if shown:
                    facts[f"extracted.{kind.lower()}.{key}"] = shown
        for f in repository.get_current_findings(case_id, kind="KYC") or []:
            for field in (getattr(f, "payload", None) or {}).get("fields") or []:
                facts[f"kyc.{str(field.get('field') or '').lower()}"] = str(field.get("status") or "").upper()
            facts["kyc.overall"] = str(getattr(f, "status", "") or "").upper()
    except Exception:  # noqa: BLE001 - an unreadable finding is a fact not on the sheet, never an invented one
        pass
    return facts


# ==========================================================================
# THE MODEL CALL
# ==========================================================================

def _prompt() -> str:
    return str(_cfg().get("prompt") or (
        "You answer questions about ONE loan case using ONLY the FACTS given. Never use outside knowledge. "
        "Reply as JSON: {\"answer\": short answer in plain words, \"keys\": [the fact keys you used], "
        "\"missing\": \"\" or the thing that is not in the facts}. If the facts do not contain the answer, "
        "set answer to \"\" and name what is missing. Copy numbers, dates and names exactly as written in the facts."))


async def _ollama(prefix: str, content: str, timeout: float) -> dict:
    import httpx

    from app.agents.applicant.copilot.semantics.llm_router import router_model
    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, with_num_ctx

    model = router_model()
    body = {"model": model, "stream": False, "format": "json", "keep_alive": keep_alive(),
            "messages": [{"role": "system", "content": prefix}, {"role": "user", "content": content}],
            "options": with_num_ctx({"temperature": 0.0, "num_predict": int(_cfg().get("num_predict", 120))})}
    if model.startswith("qwen3"):
        body["think"] = False
    async with httpx.AsyncClient(timeout=timeout + 0.5) as client:
        response = await client.post(f"{ollama_host().rstrip('/')}/api/chat", json=body)
        response.raise_for_status()
        return response.json()


async def _ask(facts: dict[str, str], question: str, generator: Any) -> tuple[dict | None, str]:
    import asyncio

    content = "FACTS:\n" + "\n".join(f"{k}: {v}" for k, v in facts.items()) + f"\n\nQUESTION: {question}"
    limit = float(_cfg().get("timeout_seconds", 3.0))
    try:
        body = await asyncio.wait_for(generator(_prompt(), content, limit), timeout=limit)
    except (asyncio.TimeoutError, TimeoutError):
        return None, "TIMEOUT"
    except Exception as exc:  # noqa: BLE001 - no model: "not recorded" is never claimed, the caller clarifies
        return None, f"ERROR:{type(exc).__name__}"
    text = str(((body or {}).get("message") or {}).get("content") or "")
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        return json.loads(text[start:end]), "OK"
    except (ValueError, TypeError):
        return None, "INVALID"


# ==========================================================================
# THE FACT CHECK
# ==========================================================================

_NUMBER = re.compile(r"(?<![A-Za-z])\d[\d,./:-]*\d|(?<![A-Za-z\d])\d(?![\d])")
_ID = re.compile(r"\b(?:CASE|APP|COAPP|QRY)-[0-9A-Z]{4,}\b", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[\s,₹]+", "", str(text or "").lower())


def check(answer: str, keys: list[str], facts: dict[str, str]) -> list[str]:
    """What in the answer is NOT on the fact sheet (empty = it passes)."""
    problems = [f"key:{k}" for k in keys or [] if k not in facts]
    sheet = _norm(" ".join(facts.values()))
    for token in _NUMBER.findall(answer or "") + _ID.findall(answer or ""):
        if _norm(token) not in sheet:
            problems.append(f"value:{token}")
    words = {str(w).lower() for w in _cfg().get("checked_words") or []}
    said = re.findall(r"[a-z_]+", (answer or "").lower())
    # a document's name lives in its KEYS ("extracted.salary_slip.employer_name"), a status in the values
    sheet_words = set(re.findall(r"[a-z]+", " ".join(list(facts.values()) + list(facts)).lower().replace("_", " ")))
    for w in said:
        if w in words and w not in sheet_words:
            problems.append(f"word:{w}")
    return problems


def _answerability(question: str) -> str | None:
    """Where a fact would come from when the case does not hold it (11d), from config."""
    said = (question or "").lower()
    for rule in _cfg().get("answerability") or []:
        if any(re.search(rf"\b{re.escape(str(w).lower())}\b", said) for w in rule.get("words") or []):
            return str(rule.get("say") or "")
    return None


async def answer(case_id: str, question: str, *, generator: Any = None, repository: Any = None) -> dict[str, Any] | None:
    """
    {"answer", "keys", "status", "ms"} -- status ANSWERED (fact-checked), NOT_RECORDED (with where it would come
    from), or None when the model was not available (the caller keeps its clarification).
    """
    started = time.perf_counter()
    hint = _answerability(question)
    facts = fact_sheet(case_id, repository)
    if not facts:
        return None
    if generator is None:
        from app.llm import availability
        from app.llm.memory import free_gb

        free = free_gb()
        if (free is not None and free < float(_cfg().get("min_free_ram_gb", 1.5))) \
                or not availability.provider_reachable():
            return {"status": "NOT_RECORDED", "answer": " ".join(x for x in (_label("not_recorded"), hint) if x),
                    "keys": [], "ms": 0.0} if hint else None
        generator = _ollama
    data, status = await _ask(facts, question, generator)
    ms = round((time.perf_counter() - started) * 1000, 1)
    if status != "OK" or not isinstance(data, dict):
        logger.info("snapshot_qa case=%s status=%s ms=%s", case_id, status, ms)
        return {"status": "NOT_RECORDED", "answer": " ".join(x for x in (_label("not_recorded"), hint) if x),
                "keys": [], "ms": ms} if hint else None
    text = str(data.get("answer") or "").strip()
    keys = [str(k) for k in data.get("keys") or [] if isinstance(k, str)]
    problems = check(text, keys, facts) if text else ["empty"]
    if text and not problems and keys:
        logger.info("snapshot_qa case=%s status=ANSWERED keys=%s ms=%s", case_id, ",".join(keys), ms)
        return {"status": "ANSWERED", "answer": text, "keys": keys, "ms": ms}
    logger.info("snapshot_qa case=%s status=DISCARDED problems=%s ms=%s", case_id, ",".join(problems[:5]), ms)
    # the model's own "missing" words are never echoed (they are unchecked text); only the configured
    # answerability line says where a fact would come from
    return {"status": "NOT_RECORDED", "answer": " ".join(x for x in (_label("not_recorded"), hint) if x),
            "keys": [], "ms": ms, "discarded": problems[:5]}


__all__ = ["FLAG", "answer", "check", "enabled", "fact_sheet"]
