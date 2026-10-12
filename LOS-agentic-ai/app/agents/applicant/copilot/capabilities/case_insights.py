"""
CASE INSIGHTS -- COUNT, RANK and COMPARE across the officer's OWN cases (2026-10-11, audit P2-C).

    "how many of my cases have a KYC problem?"      -> COUNT   (a case-list filter, counted)
    "which of my cases is closest to CPA?"          -> CLOSEST (fewest blockers first)
    "which case has more documents pending?"        -> PENDING (most pending documents first)
    "compare CASE-A and CASE-B" / "compare my cases" -> COMPARE (side by side)

DETERMINISTIC AND AUTHORISED. The rows are the case list's own (`case_list._owned` over the caller's granted cases:
a case the caller may not open is never counted, ranked or named); a case named in a comparison is authorised again
on its own. Every number is read from the store -- the documents still pending / to re-upload are the document
view's (`document_actions.build`), the KYC state is the row's. No model is called. Phrasings and sentences:
case_list.yaml `insights`.
"""
from __future__ import annotations

import re
from typing import Any

COUNT, CLOSEST, PENDING, COMPARE = "COUNT", "CLOSEST", "PENDING", "COMPARE"
_CASE_ID = re.compile(r"\bCASE-[0-9A-F]{6,}\b", re.I)


def _cfg() -> dict[str, Any]:
    from app.agents.applicant.copilot.capabilities import case_list

    return case_list.cfg().get("insights") or {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def kind_of(message: str) -> str | None:
    """Which insight the message asks for, or None (an ordinary message)."""
    if not enabled():
        return None
    text = " ".join(str(message or "").lower().split())
    shapes = _cfg().get("shapes") or {}
    for kind in (COMPARE, CLOSEST, PENDING, COUNT):
        if any(re.search(p, text) for p in shapes.get(kind.lower()) or []):
            return kind
    return None


def _say(key: str, lang: str, **values: Any) -> str:
    from app.agents.applicant.copilot.capabilities import product_flow

    return product_flow.say((_cfg().get("texts") or {}).get(key), lang, 0, **values)


def _rows(claims: dict[str, Any]) -> list[dict[str, Any]]:
    """The caller's own case rows, exactly as the case list builds them (ownership re-checked per row)."""
    from app.agents.applicant.copilot.capabilities import case_list
    from app.store import get_repository

    subject = case_list._subject(claims)
    limit = int(_cfg().get("max_cases", 200))
    apps = case_list._owned(claims, get_repository().list_granted_cases(subject, limit=limit))
    return list(case_list._rows_for(subject, apps).values())


def _metrics(row: dict[str, Any]) -> dict[str, Any]:
    """What blocks this case, from its document view (store) and its row's KYC state."""
    from app.agents.applicant.copilot.answering import document_actions

    try:
        view = document_actions.build(row["case_id"])
    except Exception:  # noqa: BLE001 - unreadable: counted as unknown, never as clear
        return {"pending": None, "documents": [], "kyc_issue": row.get("status_kind") == "KYC", "blockers": None}
    docs = [str(r.get("label") or r.get("document_type")) for r in (view.get("reupload") or []) + (view.get("pending") or [])]
    docs = list(dict.fromkeys(docs))
    kyc_issue = row.get("status_kind") == "KYC"
    return {"pending": len(docs), "documents": docs, "kyc_issue": kyc_issue,
            "blockers": len(docs) + (1 if kyc_issue else 0)}


def _line(row: dict[str, Any], m: dict[str, Any], lang: str) -> str:
    from app.agents.applicant.copilot.answering import contract

    docs = ", ".join(m["documents"][:4]) or _say("nothing", lang)
    return _say("row", lang, name=row.get("applicant_full_name") or row.get("applicant_name"), case_id=row["case_id"],
                stage=row.get("stage") or "--", status=row.get("status_label") or row.get("status") or "--",
                pending="?" if m["pending"] is None else m["pending"], documents=docs,
                kyc=_say("kyc_issue" if m["kyc_issue"] else "kyc_ok", lang)) \
        + " " + contract.link("open_case", lang, id=row["case_id"])


def answer(message: str, claims: dict[str, Any], request_id: str, lang: str = "en") -> dict[str, Any] | None:
    """The insight reply, or None when the message asks for none."""
    kind = kind_of(message)
    if kind is None:
        return None
    from app.agents.applicant.copilot.capabilities import case_list, general, workspace
    from app.security import access

    rows = _rows(claims)
    if not rows:
        return general._reply(request_id, "CASE_INSIGHT", _say("no_cases", lang), category="CASE_ONLY")
    text = " ".join(str(message or "").lower().split())
    if kind == COUNT:
        filters = (case_list.cfg().get("phrases") or {}).get("filter") or {}
        key = next((k for k, phrases in filters.items() if case_list._has(text, phrases)), None)
        if key is None:
            return None                         # "how many cases" alone: the list answers it (its own count line)
        rule = (case_list.cfg().get("filters") or {}).get(key) or {}
        chosen = [r for r in rows if (not rule.get("kinds") or r["status_kind"] in rule["kinds"])
                  and (not rule.get("over_target") or r.get("over_target"))]
        lines = [_say("count", lang, n=len(chosen), total=len(rows),
                      what=case_list._name_of("filter_names", key, lang))]
        lines += [_line(r, _metrics(r), lang) for r in chosen[: int(_cfg().get("max_listed", 5))]]
    elif kind in (CLOSEST, PENDING):
        scored = [(r, _metrics(r)) for r in rows]
        known = [(r, m) for r, m in scored if m["blockers"] is not None]
        if kind == CLOSEST:
            at_fos = [(r, m) for r, m in known if str(r.get("stage") or "").upper() == "FOS"]
            ranked = sorted(at_fos, key=lambda rm: (rm[1]["blockers"], rm[0]["case_id"]))
            head = "closest" if ranked else "none_at_fos"
        else:
            ranked = sorted(known, key=lambda rm: (-rm[1]["pending"], rm[0]["case_id"]))
            head = "most_pending" if ranked and ranked[0][1]["pending"] else "none_pending"
        lines = [_say(head, lang)] + [_line(r, m, lang) for r, m in ranked[: int(_cfg().get("max_listed", 5))]]
    else:                                       # COMPARE
        named = list(dict.fromkeys(m.upper() for m in _CASE_ID.findall(str(message or ""))))
        if named:
            mine = {r["case_id"]: r for r in rows}
            for case_id in named:
                try:
                    workspace.authorize(claims, case_id)
                except access.AccessDenied:
                    return general._reply(request_id, "CASE_INSIGHT", _say("not_yours", lang, case_id=case_id),
                                          category="CASE_ONLY")
            chosen = [mine[c] for c in named if c in mine]
        else:
            chosen = rows if len(rows) <= int(_cfg().get("compare_all_up_to", 3)) else []
        if len(chosen) < 2:
            return general._reply(request_id, "CASE_INSIGHT", _say("name_two", lang), category="CASE_ONLY")
        lines = [_say("compare", lang, n=len(chosen))] + [_line(r, _metrics(r), lang) for r in chosen]
    reply = general._reply(request_id, "CASE_INSIGHT", "\n".join(lines), category="CASE_ONLY")
    reply.update(query_type="CASE_INSIGHT", response_source="STRUCTURED", tools_invoked=["case.list", "documents.get"],
                 insight={"kind": kind})
    return reply


__all__ = ["CLOSEST", "COMPARE", "COUNT", "PENDING", "answer", "enabled", "kind_of"]
