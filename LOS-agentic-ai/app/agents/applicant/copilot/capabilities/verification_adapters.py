"""
VERIFICATION ADAPTERS -- which EXISTING service verifies each document type,
and the one contract every result is normalized into. The Copilot never
verifies anything itself: it asks the service that owns the type.

THE ACTUAL SERVICES (inspected, not assumed):

  upload path      POST /api/v1/fos/documents  (and the FOS copilot's
                   UPLOAD_DOCUMENT action) -> los.flow._process_one ->
                   document_agent.workflow (common upload gate, OCR once,
                   classification, the verification agent's checks, scoring)
                   -- slot- and party-aware; every document of an upload is
                   processed CONCURRENTLY (asyncio.gather)
  standalone       POST /api/v1/verify -- the verification agent, one OCR
                   pass; the class is detected (or asserted by expected_type)
  financial        POST /api/v1/financial/verify
  specialists      sale deed, business evidence, signatures
                   (los.flow._SPECIALIST_AGENTS)
  background read  store/ocr_queue.py (durable jobs) -- BANK_STATEMENT only:
                   the one type whose read can outlive a request

There is NO per-type endpoint (no /pan/verify-pan, /voter-id/verify, ...) in
this service; the adapters map every type onto the services above.

WHAT A RESULT MEANS. The verification agent reports whether a document is
readable, well-formed and current -- `authenticity_checked` is always false,
and no type is checked against an issuing authority here. The normalized
contract says so (`authoritative: false`); nothing is called "government
verified".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Adapter:
    document_type: str
    service: str                  # the existing service that verifies it
    endpoint: str                 # the standalone HTTP door, when one exists
    background_reader: bool       # can be (re-)read by a durable job, without a new upload
    statuses: tuple[str, ...]
    score: str                    # where a score comes from, or that none exists
    limitation: str | None = None

    def public(self) -> dict[str, Any]:
        return {"document_type": self.document_type, "service": self.service, "endpoint": self.endpoint,
                "background_reader": self.background_reader, "statuses": list(self.statuses),
                "score": self.score, "authoritative": False, "limitation": self.limitation}


_STATUSES = ("PASS", "REVIEW", "FAIL", "SKIPPED")
_SCORED = "verification.scoring.assess (0-100 with confidence), persisted on the VERIFICATION finding"


def registry() -> dict[str, Adapter]:
    """Every document type the verification agent is configured for, mapped to
    its existing service. Built from the agent's own configuration."""
    from app.agents.los import flow
    from app.agents.verification import configuration
    from app.store import ocr_queue

    classes = configuration().get("classes") or {}
    out: dict[str, Adapter] = {}
    for group, types in classes.items():
        for document_type in types:
            specialist = flow.specialist_for(document_type)
            limitation = None
            if document_type == "AADHAAR":
                limitation = ("Classified and reported as needing EXTERNAL verification; the number is "
                              "never read or held here.")
            elif document_type not in ocr_queue.READABLE_TYPES:
                limitation = ("Verified on upload; a re-verification needs the document uploaded again "
                              "(no background reader for this type).")
            out[document_type] = Adapter(
                document_type=document_type,
                service=(f"specialist:{specialist}" if specialist
                         else "document_agent.workflow + verification agent"),
                endpoint="POST /api/v1/financial/verify" if group == "financial" else "POST /api/v1/verify",
                background_reader=document_type in ocr_queue.READABLE_TYPES,
                statuses=_STATUSES, score=_SCORED, limitation=limitation)
    # THE SPECIALISTS the upload path routes to by declared type (signatures,
    # business evidence), which the verification agent's classes do not list
    for document_type, specialist in flow._SPECIALIST_AGENTS.items():
        if document_type in out:
            continue
        signature = specialist == "signature_verification"
        out[document_type] = Adapter(
            document_type=document_type, service=f"specialist:{specialist}",
            endpoint="upload path (POST /api/v1/fos/documents, declared type); no standalone endpoint",
            background_reader=False, statuses=("PASS", "REVIEW", "FAIL"),
            score=("comparison_score, only when compared against a readable reference signature"
                   if signature else "as the specialist reports it; null otherwise"),
            limitation=("PASS requires a reference signature to compare against; without one the result "
                        "is REVIEW (comparison NOT_COMPARABLE) -- a present signature is never called "
                        "genuine." if signature else
                        "Verified on upload by its specialist; re-verification needs a new upload."))
    return out


def adapter_for(document_type: str | None) -> Adapter | None:
    return registry().get(str(document_type or "").upper())


def normalize(result: Any, *, source: str) -> dict[str, Any]:
    """
    Any verification result -> the Copilot's common contract. Missing values
    stay missing (null): no default score, no invented reason.
    """
    data = result.model_dump() if hasattr(result, "model_dump") else dict(result or {})
    status = str(getattr(data.get("status"), "value", data.get("status")) or "").upper() or None
    codes = [str(c) for c in data.get("reason_codes") or []]
    score = data.get("score")
    confidence = data.get("confidence")
    from app.agents.applicant.copilot.answering import structured

    decision = {"PASS": "PASS", "REVIEW": "REVIEW", "FAIL": "FAIL", "SKIPPED": "SKIPPED"}.get(status or "")
    return {
        "document_type": data.get("document_type"),
        "verification_status": status,
        "decision": decision,
        # a score only where the service reports one (the /verify agent reports
        # OCR confidence, not a verification score)
        "score": score if isinstance(score, (int, float)) else None,
        "confidence": confidence if isinstance(confidence, (int, float)) else None,
        "reason_code": codes[0] if codes else None,
        "reason_codes": codes,
        "reason": structured._reason(codes),
        "next_action": structured.next_step(decision or "REVIEW", data.get("document_type")) if decision else None,
        "authoritative": False,
        "authenticity_checked": bool(data.get("authenticity_checked", False)),
        "source": source,
        "processing_ms": data.get("processing_ms"),
    }


def inventory() -> list[dict[str, Any]]:
    return [a.public() for a in registry().values()]


__all__ = ["Adapter", "adapter_for", "inventory", "normalize", "registry"]
