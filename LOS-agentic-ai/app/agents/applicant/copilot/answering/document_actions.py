"""
THE DOCUMENTS THAT NEED ACTION (Phase 3 step 4; flag COPILOT_DOCUMENT_ACTIONS, default off).

"kaunse docs baaki hai?" / "what is pending?" shows only what someone must act on,
built from the case store alone -- never from a model:

    Upload again       REJECTED documents, with the verification's own reasons; and the
                       documents a failed KYC check names, with the field and what each
                       document said (the KYC finding's recorded values, quoted exactly)
    Still pending      mandatory checklist slots with no document
    Under review       REVIEW documents and documents still being verified -- information
                       only, never offered for upload

VERIFIED documents (unless a KYC mismatch names them) and SUPERSEDED documents are never
listed. Every row is grouped by party (applicant / co-applicant).
"""

from __future__ import annotations

import os
import re
from typing import Any

FLAG = "COPILOT_DOCUMENT_ACTIONS"

_FIELD_LABEL = {"NAME": "Name", "DATE_OF_BIRTH": "Date of birth", "PAN_NUMBER": "PAN number",
                "FATHER_NAME": "Father's name", "ADDRESS": "Address"}
_KYC_FIELDS = set(_FIELD_LABEL)
_ROLE_LABEL = {"PRIMARY_APPLICANT": "Applicant", "CO_APPLICANT": "Co-applicant"}

#: English defaults; languages.yaml carries hi / hi-Latn / mr under the same keys
_EN = {
    "docact_kyc_heading": "Your KYC verification needs attention:",
    "docact_reupload_heading": "Please upload the correct documents:",
    "docact_pending_heading": "Still pending:",
    "docact_review_heading": "Under review (no action needed):",
    "docact_none": "No document needs any action right now.",
    "docact_mismatch": "{field} mismatch: {values}",
    "docact_mismatch_check": "{field} needs a check across {documents}",
    "docact_shows": "{document} shows \"{value}\"",
    "docact_upload": "Upload",
    "docact_being_verified": "being verified",
    "docact_kyc_status_heading": "KYC:",
    "docact_kyc_status_REVIEW": "KYC needs review",
    "docact_kyc_status_FAIL": "KYC failed",
}


def _policy() -> dict[str, Any]:
    """applicant_agent.yaml chatbot.document_actions (user, 2026-10-07: anything in review or failed is listed)."""
    from app.agents.applicant import config

    return config.chatbot("document_actions") or {}


def enabled() -> bool:
    return (os.getenv(FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _t(key: str, language: str | None, **values: Any) -> str:
    """The template in the reply's language, else English -- values inserted as given."""
    said = None
    if language and language != "en":
        from app.agents.applicant import language as languages

        said = languages.localized(key, language, **values)
    return said or _EN[key].format(**values)


#: KYC field -> the localizer's word key (answering/localize.py _FIELD_WORDS / _EXTRA_FIELD_WORDS)
_FIELD_WORD_KEY = {"NAME": "name", "DATE_OF_BIRTH": "date of birth", "FATHER_NAME": "father's name",
                   "ADDRESS": "address", "PAN_NUMBER": "PAN number"}


def _field_label(field: str, language: str | None) -> str:
    """The field's name in the reply's language (the same words the KYC answers use), else English."""
    if language and language != "en":
        from app.agents.applicant.copilot.answering import localize

        words = {**(localize._FIELD_WORDS.get(language) or {}), **(localize._EXTRA_FIELD_WORDS.get(language) or {})}
        word = words.get(_FIELD_WORD_KEY.get(field, ""))
        if word:
            return word[:1].upper() + word[1:]
    return _FIELD_LABEL[field]


def _value(x: Any) -> str:
    return str(getattr(x, "value", x) or "")


def _readable(document_type: Any) -> str:
    from app.agents.applicant.copilot.answering.answer import _readable as readable

    return readable(document_type)


def _role(raw: Any) -> str:
    role = _value(raw).upper()
    return role if role in _ROLE_LABEL else "PRIMARY_APPLICANT"


def _missing_slots(case_id: str, documents: list, repository: Any) -> list[str]:
    from app.agents.applicant import workflow

    try:
        checklist = workflow.build_checklist(repository.get_application(case_id), documents)
    except Exception:  # noqa: BLE001 - no policy for the product: nothing is imposed
        return []
    return [str(e["slot"]) for e in checklist if e.get("mandatory", True) and e.get("status") == "MISSING"]


def build(case_id: str, *, party: str | None = None, repository: Any = None) -> dict[str, Any]:
    """
    {"reupload", "pending", "under_review", "kyc_issues"} for one case, from the store.
    `party` (PRIMARY_APPLICANT / CO_APPLICANT) keeps only that party's rows.
    """
    from app.agents.applicant.copilot.answering.answer import _explained
    from app.store import get_repository

    repository = repository or get_repository()
    stored = [d for d in repository.list_documents(case_id) or []
              if _value(getattr(d, "status", "")).upper() != "SUPERSEDED"]
    role_of_party = {getattr(d, "party_id", None): _role(getattr(d, "party_role", None)) for d in stored}

    # REVIEW documents: offered for upload again (with their reasons) when the policy says so -- a
    # reviewer flagged them; otherwise information only, as before
    review_reupload = bool(_policy().get("review_needs_reupload", False))
    reupload: list[dict[str, Any]] = []
    under_review: list[dict[str, Any]] = []
    for d in stored:
        status = _value(getattr(d, "status", "")).upper()
        role = _role(getattr(d, "party_role", None))
        row = {"party": role, "party_label": _ROLE_LABEL[role], "document_type": d.document_type,
               "label": _readable(d.document_type), "document_id": d.document_id}   # 6i: view / query targets
        if status == "REJECTED" or (status == "REVIEW" and review_reupload):
            reasons = [r for r in (_explained(c) for c in (getattr(d, "reason_codes", None) or [])) if r]
            reupload.append({**row, "reasons": reasons[:2],
                             "source": "VERIFICATION" if status == "REJECTED" else "VERIFICATION_REVIEW"})
        elif status in ("REVIEW", "UPLOADED", "PROCESSING"):
            under_review.append({**row, "state": "REVIEW" if status == "REVIEW" else "PROCESSING"})

    # KYC: every failed / reviewed identity field, with the values the record kept
    kyc_issues: list[dict[str, Any]] = []
    kyc_status: list[dict[str, Any]] = []
    try:
        findings = repository.get_current_findings(case_id, kind="KYC") or []
    except Exception:  # noqa: BLE001 - unreadable KYC: no KYC rows, never invented ones
        findings = []
    for finding in findings:
        role = role_of_party.get(getattr(finding, "party_id", None)) or "PRIMARY_APPLICANT"
        verdict = str(getattr(finding, "status", "") or "").upper()
        if verdict in ("REVIEW", "FAIL") and _policy().get("kyc_status_line", False):
            # THE PARTY'S KYC VERDICT ITSELF -- shown even when the finding names no field
            reasons = [r for r in (_explained(c) for c in (getattr(finding, "reason_codes", None) or [])) if r]
            kyc_status.append({"party": role, "party_label": _ROLE_LABEL[role], "status": verdict,
                               "reasons": reasons[:2]})
        for field in (getattr(finding, "payload", None) or {}).get("fields") or []:
            name = str((field or {}).get("field") or "").upper()
            status = str((field or {}).get("status") or "").upper()
            if name not in _KYC_FIELDS or status not in ("FAIL", "REVIEW", "PARTIAL"):
                continue
            sources = [s for s in field.get("sources") or [] if isinstance(s, dict) and s.get("document_type")]
            kyc_issues.append({"party": role, "party_label": _ROLE_LABEL[role], "field": name,
                               "field_label": _FIELD_LABEL[name], "status": status,
                               "values": [{"document_type": s["document_type"], "label": _readable(s["document_type"]),
                                           "value": s.get("value")} for s in sources]})
            # the documents whose values disagree are the ones to upload again
            if status == "FAIL":
                for s in sources:
                    if s.get("value") and not any(r["party"] == role and r["document_type"] == s["document_type"]
                                                  for r in reupload):
                        reupload.append({"party": role, "party_label": _ROLE_LABEL[role],
                                         "document_type": s["document_type"], "label": _readable(s["document_type"]),
                                         "reasons": [], "source": "KYC"})

    from app.agents.applicant import workflow

    if workflow.coapp_mandatory_enabled():
        # EVERY PARTY'S missing mandatory documents (LOS_COAPP_MANDATORY_DOCS, step 5b)
        pending = []
        for checklist in workflow.party_checklists(repository.get_application(case_id), stored):
            role = checklist["party_role"]          # (not `party`: that is this function's filter)
            pending += [{"party": role, "party_label": _ROLE_LABEL[role], "document_type": e["slot"],
                         "label": _readable(e["slot"])}
                        for e in checklist["checklist"] if e.get("mandatory", True) and e.get("status") == "MISSING"]
    else:
        primary = [d for d in stored if _role(getattr(d, "party_role", None)) == "PRIMARY_APPLICANT"]
        pending = [{"party": "PRIMARY_APPLICANT", "party_label": "Applicant", "document_type": slot,
                    "label": _readable(slot)} for slot in _missing_slots(case_id, primary, repository)]

    application = repository.get_application(case_id)
    if workflow.signature_rule_applies(application)[0]:
        # MANDATORY SIGNATURE (LOS_SIGNATURE_MANDATORY, step 5c): signature rows come from the
        # PRESENCE result readiness uses, not the authenticity status (REVIEW without a specimen)
        signature = workflow.SIGNATURE_TYPES
        reupload = [r for r in reupload if str(r["document_type"]).upper() not in signature]
        under_review = [r for r in under_review if str(r["document_type"]).upper() not in signature]
        for item in workflow.signature_items(application, stored):
            role = _role(item.get("party_role"))
            row = {"party": role, "party_label": _ROLE_LABEL[role], "document_type": "SIGNATURE",
                   "label": _readable("SIGNATURE")}
            if item["code"] in ("SIGNATURE_REJECTED", "SIGNATURE_NOT_CHECKED"):
                reupload.append({**row, "reasons": (item.get("reasons") or [item["detail"]])[:2],
                                 "source": "SIGNATURE_PRESENCE"})
            elif item["code"] == "SIGNATURE_UNDER_REVIEW":
                under_review.append({**row, "state": "REVIEW"})
            elif item["code"] == "DOCUMENT_MISSING":
                pending.append(row)

    def keep(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in rows if party is None or r["party"] == party]

    # the verdict line only where no field-level mismatch already says it for that party
    detailed = {i["party"] for i in kyc_issues if i["values"]}
    kyc_status = [k for k in kyc_status if k["party"] not in detailed]

    # ONE ROW PER DOCUMENT: a slot already listed to upload again / under review is not "still pending" too
    listed = {(r["party"], str(r["document_type"]).upper()) for r in reupload + under_review}
    pending = [r for r in pending if (r["party"], str(r["document_type"]).upper()) not in listed]

    out = {"reupload": keep(reupload), "pending": keep(pending), "under_review": keep(under_review),
           "kyc_issues": keep(kyc_issues), "kyc_status": keep(kyc_status)}
    for row in out["reupload"] + out["pending"]:
        row["action"] = {"type": "UPLOAD_DOCUMENT", "document_type": row["document_type"], "party": row["party"],
                         "label": f"Upload {row['label']}"}
    return out


def render(view: dict[str, Any], language: str | None = None) -> dict[str, Any]:
    """(answer text, the 1-2 words worth emphasis) from a built view. Compact; the payload holds the rest."""
    two = len({r["party"] for k in ("reupload", "pending", "under_review", "kyc_issues", "kyc_status")
               for r in view.get(k) or []}) > 1

    def name(row: dict[str, Any]) -> str:
        return f"{row['party_label']}'s {row['label']}" if two else row["label"]

    lines: list[str] = []
    emphasis: list[str] = []
    for k in view.get("kyc_status") or []:
        # the KYC verdict first: "⚠️ KYC needs review — <reason>" (per party when two)
        said = _t(f"docact_kyc_status_{k['status']}", language)
        who = f"{k['party_label']}: " if two else ""
        reason = f" — {k['reasons'][0]}" if k.get("reasons") else ""
        lines.append(f"⚠️ {who}{said}{reason}")
        emphasis.append("KYC")
    issues = [i for i in view["kyc_issues"] if i["values"]]
    if issues:
        lines.append(_t("docact_kyc_heading", language))
        for i in issues[:4]:
            label = _field_label(i["field"], language)
            field = f"{i['party_label']}'s {label[:1].lower() + label[1:]}" if two else label
            with_values = [v for v in i["values"] if v.get("value")]
            if len(with_values) >= 2:
                shown = ", ".join(_t("docact_shows", language, document=v["label"], value=v["value"])
                                  for v in with_values[:3])
                lines.append("• " + _t("docact_mismatch", language, field=field, values=shown))
            else:
                docs = " / ".join(v["label"] for v in i["values"][:3])
                lines.append("• " + _t("docact_mismatch_check", language, field=field, documents=docs))
            emphasis.append(label)                       # the word as shown, so step 5 can bold it
    upload = _t("docact_upload", language)
    if view["reupload"]:
        lines.append(_t("docact_reupload_heading", language))
        for r in view["reupload"]:
            reason = f" — {r['reasons'][0]}" if r.get("reasons") else ""
            lines.append(f"📄 {name(r)} [{upload}]{reason}")
            emphasis.append(r["label"])
    if view["pending"]:
        lines.append(_t("docact_pending_heading", language))
        lines += [f"📄 {name(r)} [{upload}]" for r in view["pending"]]
        emphasis += [r["label"] for r in view["pending"]]
    if view["under_review"]:
        lines.append(_t("docact_review_heading", language))
        verifying = _t("docact_being_verified", language)
        lines += [f"⏳ {name(r)}" + (f" ({verifying})" if r["state"] == "PROCESSING" else "")
                  for r in view["under_review"]]
    if not lines:
        lines.append(_t("docact_none", language))
    seen: list[str] = []
    for word in emphasis:
        if word not in seen:
            seen.append(word)
    return {"answer": "\n".join(lines), "emphasis": seen[:2]}


DIAGNOSE_FLAG = "COPILOT_VERIFY_DIAGNOSE"


def diagnose_enabled() -> bool:
    return (os.getenv(DIAGNOSE_FLAG, "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _diagnose_cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("verify_diagnose") or {}


def asks_to_verify(message: str) -> bool:
    """6f: "verify karna hai" / "kya upload karu" / "KYC complete karna hai" (phrases in config, whole words)."""
    said = " " + " ".join(re.sub(r"[^\w\sऀ-ॿ]", " ", str(message or "").lower()).split()) + " "
    return any(f" {' '.join(str(p).lower().split())} " in said for p in _diagnose_cfg().get("phrases") or [])


def all_done_text() -> str:
    return str(_diagnose_cfg().get("all_done") or "✅ All documents are verified.")


__all__ = ["DIAGNOSE_FLAG", "FLAG", "all_done_text", "asks_to_verify", "build", "diagnose_enabled", "enabled",
           "render"]
