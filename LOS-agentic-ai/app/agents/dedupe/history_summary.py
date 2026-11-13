"""
THE EXPLANATION of a customer-history assessment -- deterministic first, the model last (Phase B, 2026-10-22).

  facts()          the ONLY thing the model sees: the verified, masked result (match, masked refs, loan states, totals
                   only where computed, rule outcomes, the final outcome, next actions). No PAN, mobile, name, DOB,
                   full AP / PR code or account number exists in it.
  deterministic()  the explanation written from those facts by code -- always available.
  explain()        the model's wording, accepted only if the unified validator (app/security/output_validation.py,
                   surface customer_history_summary) and this module's checks pass: no number or date absent from the
                   facts, no decision language, no full code / PAN / mobile, no "no loans" unless the history is an
                   authoritative none, no outcome other than the computed one. Anything else -- a timeout, the model
                   down or switched off, a rejected text -- returns the deterministic explanation, with the reason.
The model never changes the outcome: it is not given a way to.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)
Generator = Callable[[dict[str, Any]], str]

_FULL_CODE = re.compile(r"\b(AP|PR)\d{8}\b", re.I)
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.I)
_MOBILE = re.compile(r"(?<!\d)[6-9]\d{9}(?!\d)")
_NO_LOANS = re.compile(r"\bno (previous |prior |existing |other )?loans?\b|\bzero loans?\b", re.I)
_EMPTY_OK = ("NO_PREVIOUS_LOANS", "NOT_APPLICABLE")
_OUTCOMES = ("READY_FOR_DECISION", "REVIEW_REQUIRED", "DATA_INSUFFICIENT")


def _cfg() -> dict[str, Any]:
    from app.agents.dedupe import history_policy

    return history_policy.policy().get("summary") or {}


def facts(result: dict[str, Any]) -> dict[str, Any]:
    h, e = result["history"], result["evaluation"]
    a = h.get("aggregates") or {}

    def total(name: str) -> dict[str, Any] | None:
        t = a.get(name)
        return {"status": t.get("status"), "value_rupees": t.get("value")} if t else None

    return {
        "match": h["customer"]["match"], "customer_ref": h["customer"]["customer_ref"],
        "history_status": h["history_status"], "loan_count": h.get("loan_count", 0),
        "loans_withheld": h.get("loans_withheld"),
        "loans": [{"ref": x["prospect_ref"], "scheme": x.get("scheme"), "status": x["effective_status"],
                   "status_source": x.get("status_source")} for x in h.get("loans") or []],
        "active_loans": a.get("active_loans"), "active_exposure": total("active_exposure"),
        "monthly_emi_burden": total("monthly_emi_burden"),
        "worst_dpd_12m": (a.get("worst_dpd_12m") or {}).get("value"),
        "requested": result.get("requested"),
        # the FINAL outcome (history policy + the LOS dedupe), not the history policy's alone
        "outcome": result.get("outcome", e["outcome"]), "reason_codes": result.get("reason_codes", e["reason_codes"]),
        "next_actions": result.get("next_actions", e["next_actions"]),
        "rules_triggered": [r["rule_id"] for r in e["rules"] if r["outcome"] == "TRIGGERED"],
        "rules_not_evaluated": [r["rule_id"] for r in e["rules"] if r["outcome"] == "NOT_EVALUATED"],
        # checks waiting only for a limit the business has not set: said once, never offered as the officer's next step
        # the page's ONE decision: the dedupe workflow recommendation (dedupe_status.py), when the agent produced one
        "recommendation": ((result.get("dedupe") or {}).get("workflow_recommendation") or {}).get("recommendation"),
        "dedupe_invoked": (result.get("dedupe") or {}).get("invoked"),
        # each co-applicant's own outcome (party_eval.py) -- roles and outcomes only, never an identifier
        "co_applicants": [{"status": p.get("status"), "existing_customer": bool(p.get("existing_customer")),
                           "reasons": [r for r in p.get("match_reasons") or [] if not r.endswith("_MATCHES_EXISTING_CUSTOMER")]}
                          for p in ((result.get("dedupe") or {}).get("parties") or []) if p.get("role") == "CO_APPLICANT"],
        "limits_not_set": [r["next_action"] for r in e["rules"] if r["outcome"] == "NOT_EVALUATED"
                           and r.get("reason") == "THRESHOLD_UNCONFIGURED" and r.get("next_action")],
    }


_MATCH_TEXT = {
    "CONFIRMED_MATCH": "MIFIN confirmed an existing customer ({ref}).",
    "POTENTIAL_MATCH": "MIFIN found only a potential match ({ref}); the identity is not confirmed.",
    "AMBIGUOUS": "MIFIN returned more than one possible customer; the identity is not resolved.",
    "NO_MATCH": "MIFIN found no existing customer for this applicant.",
    "SOURCE_ERROR": "MIFIN returned an error; the customer check could not be completed.",
    "UNAVAILABLE": "MIFIN could not be reached; the customer check could not be completed.",
    "MALFORMED": "MIFIN's answer could not be read; the customer check could not be completed.",
}
_HISTORY_TEXT = {
    "NO_PREVIOUS_LOANS": "MIFIN lists no previous loans for this customer.",
    "NOT_APPLICABLE": "There is no loan history to check for a new customer.",
    "NOT_ATTRIBUTED": "Loan records were not attributed to this applicant because the match is not confirmed.",
    "UNAVAILABLE": "The loan history is unknown because the source was unavailable.",
    "UNKNOWN": "MIFIN did not send the loan section, so the loan history is unknown.",
}
_RECOMMENDATION_TEXT = {
    "PROCEED_TO_NEXT_STAGE": "The dedupe check does not stop this application: it continues through the normal LOS "
                             "checks (this is not an approval).",
    "HOLD_FOR_REVIEW": "The application should be held for an officer's review of the records listed.",
    "BLOCK_APPLICATION": "The approved duplicate rule stops this application.",
    "UNABLE_TO_DECIDE": "The policy does not yet say what to do here; refer to the business owner.",
}
_NEW_CUSTOMER_TEXT = "This is a new customer, so the dedupe check was not needed."

_OUTCOME_TEXT = {
    "READY_FOR_DECISION": "The history is complete for a credit decision (this is not an approval).",
    "REVIEW_REQUIRED": "The case needs review before a credit decision.",
    "DATA_INSUFFICIENT": "There is not enough verified information for a credit decision yet.",
}


def deterministic(result: dict[str, Any]) -> str:
    f = facts(result)
    parts = [_MATCH_TEXT.get(f["match"], "The customer check finished.").format(ref=f["customer_ref"] or "")]
    if f["history_status"] in _HISTORY_TEXT:
        parts.append(_HISTORY_TEXT[f["history_status"]])
    else:
        states: dict[str, int] = {}
        for loan in f["loans"]:
            states[loan["status"].lower()] = states.get(loan["status"].lower(), 0) + 1
        parts.append(f"{f['loan_count']} previous loan record(s) were found: "
                     + ", ".join(f"{n} {s}" for s, n in sorted(states.items())) + ".")
        exposure = f["active_exposure"] or {}
        if exposure.get("status") == "COMPLETE":
            parts.append(f"Live exposure is Rs {_inr(exposure['value_rupees'])}"
                         + (f" with a monthly EMI of Rs {_inr(f['monthly_emi_burden']['value_rupees'])}"
                            if (f["monthly_emi_burden"] or {}).get("status") == "COMPLETE" else "") + ".")
        elif exposure.get("status") in ("PARTIAL", "NOT_EVALUATED"):
            parts.append("Live exposure could not be fully calculated from verified data.")
    if f["rules_triggered"]:
        raised = [c for c in f["reason_codes"] if ":" not in str(c)]   # "KIND:RULE" codes are checks NOT evaluated
        if raised:
            parts.append("Checks raised: " + ", ".join(_readable(c) for c in raised[:4]) + ".")
    waiting = f.get("limits_not_set") or []
    unverified = len(f["rules_not_evaluated"]) - len(waiting)
    if waiting:
        parts.append(f"{len(waiting)} policy check(s) wait for limits the business has not set yet.")
    if unverified > 0:
        parts.append(f"{unverified} policy check(s) could not be evaluated because the data is not verified.")
    if f.get("dedupe_invoked") is False:
        parts.append(_NEW_CUSTOMER_TEXT)
    cos = f.get("co_applicants") or []
    if cos:
        said = []
        for i, co in enumerate(cos, 1):
            who = f"co-applicant {i}" if len(cos) > 1 else "the co-applicant"
            if co["status"] == "CHECK_INCOMPLETE":
                said.append(f"{who} could not be fully checked ({_readable(co['reasons'][0]) if co['reasons'] else 'check incomplete'})")
            elif co["reasons"]:
                said.append(f"{who} needs review ({', '.join(_readable(r) for r in co['reasons'])})")
            else:
                said.append(f"{who} is {'an existing customer' if co['existing_customer'] else 'new'} with no duplicate found")
        parts.append(f"{len(cos)} co-applicant(s) were checked separately: " + "; ".join(said) + ".")
    # one decision on the page: the dedupe workflow recommendation when there is one, else the history outcome
    parts.append(_RECOMMENDATION_TEXT.get(f.get("recommendation") or "", _OUTCOME_TEXT[f["outcome"]]))
    officer = [a for a in f["next_actions"] if a not in set(waiting)]
    if f.get("recommendation") and f["recommendation"] != "PROCEED_TO_NEXT_STAGE":
        officer = [a for a in officer if not a.startswith("Proceed")]      # never "proceed" next to a hold
    if officer:
        parts.append("Next: " + officer[0])
    return _full_ap(" ".join(p for p in parts if p), *_codes(result))


def _codes(result: dict[str, Any]) -> tuple[str | None, str | None]:
    c = ((result.get("history") or {}).get("customer") or {})
    return c.get("customer_ref"), c.get("customer_code")


def _full_ap(text: str, masked: str | None, full: str | None) -> str:
    """The AP number in full in the officer's text (owner 2026-10-23). The model never sees it: its facts carry the
    masked reference, and this runs on text that already passed validation."""
    return text.replace(masked, full) if (text and masked and full) else text


def _inr(value: Any) -> str:
    """Rupees in the Indian grouping: 312400 -> 3,12,400 (a value that is not a number is shown as given)."""
    try:
        whole = str(int(round(float(str(value).replace(",", "")))))
    except (TypeError, ValueError):
        return str(value)
    sign, whole = ("-", whole[1:]) if whole.startswith("-") else ("", whole)
    head, tail = whole[:-3], whole[-3:]
    groups = [head[max(i - 2, 0):i] for i in range(len(head), 0, -2)][::-1]
    return sign + ",".join([g for g in groups if g] + [tail]) if head else sign + tail


def _readable(code: str) -> str:
    """A reason code as words, without the rule id: "DATA_UNAVAILABLE:H06_ADVERSE_HISTORY" -> "adverse history
    (data unavailable)", "LOAN_HISTORY_INCOMPLETE" -> "loan history incomplete"."""
    kind, _, subject = str(code).partition(":")
    words = lambda s: re.sub(r"^H\d+_", "", s).replace("_", " ").lower()  # noqa: E731
    return f"{words(subject)} ({words(kind)})" if subject else words(kind)


def _own_checks(f: dict[str, Any]) -> tuple[Callable[[str], str | None], ...]:
    def no_identifiers(text: str) -> str | None:
        for name, pattern in (("a full AP/PR code", _FULL_CODE), ("a PAN", _PAN), ("a mobile number", _MOBILE)):
            if pattern.search(text):
                return f"summary contained {name}"
        return None

    def no_invented_emptiness(text: str) -> str | None:
        if f["history_status"] not in _EMPTY_OK and _NO_LOANS.search(text):
            return "summary claimed no loans on a history that is not an authoritative none"
        return None

    def the_computed_outcome(text: str) -> str | None:
        upper = text.upper().replace(" ", "_")
        for other in _OUTCOMES:
            if other != f["outcome"] and other in upper:
                return f"summary asserted another outcome: {other}"
        return None

    return no_identifiers, no_invented_emptiness, the_computed_outcome


def validate(text: str, f: dict[str, Any]) -> tuple[bool, str]:
    from app.security import output_validation

    max_words = int(_cfg().get("max_words") or 140)
    if isinstance(text, str) and len(text.split()) > max_words:
        return False, "summary too long"
    return output_validation.validate(text, surface="customer_history_summary", truth=f,
                                      extra=_own_checks(f)).pair()


def _ollama(f: dict[str, Any]) -> str:
    import httpx

    from app.llm.config import ollama_host, ollama_model, with_num_ctx
    from app.security import guardrails

    system = ("You explain a loan-officer assessment in plain professional English, 3 to 6 sentences. Use ONLY the "
              "JSON facts. Never add a number, date, name, loan or status that is not in them; never say a missing "
              "value is zero; never approve or decline; state the given outcome and the first next action. "
              + guardrails.UNTRUSTED_NOTICE)
    from app.agents.los.summary import keep_alive

    # FAST: the model stays loaded between checks (keep_alive, as the chatbot does), a short answer is asked for
    # (summary.num_predict) and the same qwen3 4B instruct model as everywhere (summary.model or OLLAMA_MODEL)
    body = {"model": str(_cfg().get("model") or ollama_model()), "stream": False, "keep_alive": keep_alive(),
            "options": with_num_ctx({"temperature": 0.1, "num_predict": int(_cfg().get("num_predict") or 160)}),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps(guardrails.untrusted(f), default=str)}]}
    r = httpx.post(f"{ollama_host().rstrip('/')}/api/chat", json=body,
                   timeout=float(_cfg().get("timeout_seconds") or 6))
    r.raise_for_status()
    return str(((r.json() or {}).get("message") or {}).get("content") or "")


def explain(result: dict[str, Any], *, generator: Generator | None = None) -> dict[str, Any]:
    """{"text", "source": LLM | DETERMINISTIC, "fallback_reason", "points", "llm_reason_pending"}. Never raises.
    With summary.deferred (and no generator given) the model is NOT called here: the answer is not held up by it."""
    out = _explain(result, generator=generator)
    out["text"] = _full_ap(out["text"], *_codes(result))
    out["points"] = reason_points(result)
    out["llm_reason_pending"] = out["fallback_reason"] == "LLM_DEFERRED"
    return out


def _explain(result: dict[str, Any], *, generator: Generator | None = None) -> dict[str, Any]:
    f = facts(result)
    fallback = deterministic(result)
    if not _cfg().get("llm_enabled", True) and generator is None:
        return {"text": fallback, "source": "DETERMINISTIC", "fallback_reason": "LLM_DISABLED"}
    if generator is None:
        from app.llm import availability

        if not availability.provider_reachable():
            return {"text": fallback, "source": "DETERMINISTIC", "fallback_reason": "LLM_UNAVAILABLE"}
        if _cfg().get("deferred"):           # reachable: the page asks for the model's points after it shows
            return {"text": fallback, "source": "DETERMINISTIC", "fallback_reason": "LLM_DEFERRED"}
        generator = _ollama
    try:
        text = generator(f)
    except Exception as exc:  # noqa: BLE001 - timeout / transport / model error: the deterministic text stands
        logger.info("history summary: model failed (%s)", type(exc).__name__)
        return {"text": fallback, "source": "DETERMINISTIC", "fallback_reason": f"LLM_ERROR:{type(exc).__name__}"}
    ok, why = validate(text, f)
    if not ok:
        logger.info("history summary: model text rejected (%s)", why)
        return {"text": fallback, "source": "DETERMINISTIC", "fallback_reason": f"LLM_REJECTED:{why}"}
    return {"text": why, "source": "LLM", "fallback_reason": None}


# ---------------------------------------------------------------- the reason, in 3 points
def reason_points(result: dict[str, Any]) -> list[str]:
    """WHY the recommendation is what it is, in at most summary.reason_points plain points -- from the verified result
    only: the recommendation's own reason, the evidence behind it, the next step."""
    f = facts(result)
    d = result.get("dedupe") or {}
    w = d.get("workflow_recommendation") or {}
    n = int(_cfg().get("reason_points") or 3)
    rec = f.get("recommendation")
    points: list[str] = []
    if f.get("dedupe_invoked") is False:
        points.append(_NEW_CUSTOMER_TEXT)
    points.append(_RECOMMENDATION_TEXT.get(rec or "", _OUTCOME_TEXT[f["outcome"]]))
    evidence = [_readable(r) for r in d.get("match_reasons") or [] if not str(r).endswith("_MATCHES_EXISTING_CUSTOMER")]
    cos = f.get("co_applicants") or []
    for i, co in enumerate(cos, 1):
        who = f"co-applicant {i}" if len(cos) > 1 else "the co-applicant"
        evidence += [f"{who}: {_readable(r)}" for r in co["reasons"]]
    if evidence:
        points.append("Found: " + "; ".join(dict.fromkeys(evidence[:3])) + ".")
    else:
        points.append(_MATCH_TEXT.get(f["match"], "The customer check finished.").format(ref=f["customer_ref"] or ""))
    officer = [a for a in f["next_actions"] if a not in set(f.get("limits_not_set") or [])]
    if rec and rec != "PROCEED_TO_NEXT_STAGE":
        officer = [a for a in officer if not a.startswith("Proceed")]
    step = w.get("next_action") or (officer[0] if officer else None)
    if step:
        points.append("Next: " + str(step).rstrip(".") + ".")
    masked, full = _codes(result)
    return [_full_ap(p, masked, full) for p in dict.fromkeys(points) if p][:n]


_REASONS: dict[str, dict[str, Any]] = {}
_REASONS_LOCK = threading.Lock()


def remember(result: dict[str, Any], subject: str | None) -> None:
    """Keep one check's FACTS (masked -- never an identifier) for its reason call, bound to the caller. In memory,
    bounded (summary.reason_cache_max), expiring (summary.reason_cache_seconds)."""
    cfg = _cfg()
    now = time.monotonic()
    entry = {"facts": facts(result), "points": reason_points(result), "case_id": result.get("case_id"),
             "subject": subject, "expires": now + float(cfg.get("reason_cache_seconds") or 900), "answer": None,
             "codes": _codes(result),
             "recommendation": ((result.get("dedupe") or {}).get("workflow_recommendation") or {}).get("recommendation")}
    with _REASONS_LOCK:
        for k in [k for k, v in _REASONS.items() if v["expires"] < now]:
            _REASONS.pop(k, None)
        while len(_REASONS) >= int(cfg.get("reason_cache_max") or 200):
            _REASONS.pop(next(iter(_REASONS)))
        _REASONS[result["request_id"]] = entry


class ReasonNotFound(LookupError):
    """No check with this request id for this caller (expired, never made, or someone else's -- one answer)."""


def _ollama_points(f: dict[str, Any]) -> list[str]:
    import httpx

    from app.agents.los.summary import keep_alive
    from app.llm.config import ollama_host, ollama_model, with_num_ctx
    from app.security import guardrails

    cfg = _cfg()
    n = int(cfg.get("reason_points") or 3)
    system = (f"You explain to a loan officer WHY a dedupe check gave its recommendation. Reply with JSON "
              f'{{"points": [...]}}: exactly {n} short points, each one plain sentence of at most '
              f"{int(cfg.get('reason_point_max_words') or 24)} words. Point 1: the recommendation and its main reason. "
              "Point 2: the evidence found. Point 3: what the officer should do next. Use ONLY the JSON facts; never "
              "add a number, date, name, loan or status that is not in them; never approve or decline. "
              + guardrails.UNTRUSTED_NOTICE)
    body = {"model": str(cfg.get("model") or ollama_model()), "stream": False, "keep_alive": keep_alive(),
            "format": "json",
            "options": with_num_ctx({"temperature": 0.1, "num_predict": int(cfg.get("reason_num_predict") or 200)}),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps(guardrails.untrusted(f), default=str)}]}
    r = httpx.post(f"{ollama_host().rstrip('/')}/api/chat", json=body,
                   timeout=float(cfg.get("reason_timeout_seconds") or 40))
    r.raise_for_status()
    content = str(((r.json() or {}).get("message") or {}).get("content") or "")
    points = (json.loads(content) or {}).get("points")
    if not isinstance(points, list):
        raise ValueError("no points list")
    return [str(p).strip() for p in points]


def _check_points(points: list[str], f: dict[str, Any]) -> tuple[bool, str]:
    cfg = _cfg()
    n = int(cfg.get("reason_points") or 3)
    limit = int(cfg.get("reason_point_max_words") or 24)
    if len(points) != n or any(not p for p in points):
        return False, f"expected {n} points"
    if any(len(p.split()) > limit + 6 for p in points):
        return False, "a point is too long"
    return validate(" ".join(points), f)


def reason(request_id: str, subject: str | None, *, generator: Callable[[dict[str, Any]], list[str]] | None = None
           ) -> dict[str, Any]:
    """{"points", "source": LLM | DETERMINISTIC, "fallback_reason", "recommendation"} for one earlier check of THIS
    caller. The model's points are accepted only if they pass the same checks as the summary; else the
    deterministic points. Answered once per check (cached). Raises ReasonNotFound."""
    with _REASONS_LOCK:
        entry = _REASONS.get(request_id)
        if entry is None or entry["expires"] < time.monotonic() or entry["subject"] != subject:
            raise ReasonNotFound(request_id)
        if entry["answer"] is not None:
            return entry["answer"]
    f, fallback = entry["facts"], entry["points"]

    def answer(points: list[str], source: str, why: str | None, *, final: bool = True) -> dict[str, Any]:
        out = {"points": points, "source": source, "fallback_reason": why, "recommendation": entry["recommendation"]}
        if final:
            with _REASONS_LOCK:
                entry["answer"] = out
        return out

    if not _cfg().get("llm_enabled", True) and generator is None:
        return answer(fallback, "DETERMINISTIC", "LLM_DISABLED")
    if generator is None:
        from app.llm import availability

        if not availability.provider_reachable():
            return answer(fallback, "DETERMINISTIC", "LLM_UNAVAILABLE", final=False)   # the model may come back
        generator = _ollama_points
    try:
        points = generator(f)
    except Exception as exc:  # noqa: BLE001 - timeout / transport / bad JSON: the deterministic points stand
        logger.info("dedupe reason: model failed (%s)", type(exc).__name__)
        return answer(fallback, "DETERMINISTIC", f"LLM_ERROR:{type(exc).__name__}")
    ok, why = _check_points(points, f)
    if not ok:
        logger.info("dedupe reason: model points rejected (%s)", why)
        return answer(fallback, "DETERMINISTIC", f"LLM_REJECTED:{why}")
    return answer([_full_ap(x, *entry["codes"]) for x in points], "LLM", None)


__all__ = ["ReasonNotFound", "deterministic", "explain", "facts", "reason", "reason_points", "remember", "validate"]


# EVERY SUMMARY RETURNED AS NUMBERED POINTS (2026-11-02; app/core/summary_points.py, config summary_format.yaml):
# how it is made and checked above is unchanged -- only what is returned
from app.core import summary_points as _points  # noqa: E402

_explain = _points.text_field(_explain)
