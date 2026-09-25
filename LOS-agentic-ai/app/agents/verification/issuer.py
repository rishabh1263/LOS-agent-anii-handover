"""
Issuer verification: the ADDITIONAL authenticity layer.

THREE QUESTIONS THAT MUST NEVER BE MIXED:

    DOCUMENT VALIDITY   right type, fields present, formats valid, legible,
                        in date, MRZ checks, balances reconcile. Answered by
                        the existing deterministic verifiers, which stay
                        authoritative for it. Nothing here changes them.
    AUTHENTICITY        did the issuer -- or an authorised source acting for
                        it -- confirm this document or its facts? Answered
                        HERE, and only by a provider that actually asked.
    FRAUD SIGNALS       editing software, metadata anomalies, screen photos.
                        Answered by forensics.py. They can raise a flag or a
                        REVIEW; they can NEVER establish authenticity.

THE THREE ANSWERS:

    ISSUER_CONFIRMED   a trusted provider confirmed the submitted facts.
                       issuer_verified = true. May contribute to PASS.
    ISSUER_MISMATCH    a trusted provider answered: the facts do not match.
                       The document FAILS, with the fields that disagreed.
    NOT_ESTABLISHED    everything else -- not configured, not available,
                       timed out, errored, did not answer, not attempted.
                       issuer_verified = false. Under REQUIRE_EXTERNAL this
                       is REVIEW; otherwise the existing verdict stands.

A PROVIDER CANNOT FAKE AN ANSWER. A confirmation or a mismatch without the
provider's name, its reference and a timestamp is not evidence and is
recorded as NOT_ESTABLISHED. An outage, a timeout or an exception is never a
PASS.

NO REAL PROVIDER EXISTS IN THIS BUILD. Every document type uses the `none`
stub (issuer_verification.yaml). Real integrations register with
`register_provider` and are named in configuration; no verifier changes.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class IssuerStatus(str, Enum):
    ISSUER_CONFIRMED = "ISSUER_CONFIRMED"
    ISSUER_MISMATCH = "ISSUER_MISMATCH"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


class ProviderMode(str, Enum):
    ISSUER = "issuer"                # the issuer can confirm the document or its facts
    CORROBORATION = "corroboration"  # only independent sources can corroborate it
    NONE = "none"                    # no trusted source exists for this type


# -- reason codes ---------------------------------------------------------
NOT_CONFIGURED = "ISSUER_PROVIDER_NOT_CONFIGURED"
NOT_AVAILABLE = "ISSUER_VERIFICATION_NOT_AVAILABLE"
NOT_REGISTERED = "ISSUER_PROVIDER_NOT_REGISTERED"
TIMEOUT = "ISSUER_PROVIDER_TIMEOUT"
ERROR = "ISSUER_PROVIDER_ERROR"
INCOMPLETE = "ISSUER_CONFIRMATION_INCOMPLETE"
NOT_ATTEMPTED = "ISSUER_VERIFICATION_NOT_ATTEMPTED"
MISMATCH = "ISSUER_MISMATCH"
REQUIRED_NOT_ESTABLISHED = "AUTHENTICITY_NOT_ESTABLISHED"

_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


# ==========================================================================
# THE CONTRACT
# ==========================================================================


@dataclass
class IssuerVerificationRequest:
    """What a provider is asked. Values go to the provider, never to storage."""

    document_type: str
    fields: dict[str, Any] = field(default_factory=dict)
    case_id: str | None = None
    applicant_id: str | None = None
    party_id: str | None = None
    document_id: str | None = None
    consent_id: str | None = None
    request_id: str | None = None


class IssuerVerificationResult(BaseModel):
    """The normalised answer. Field NAMES only -- never the values."""

    status: IssuerStatus = IssuerStatus.NOT_ESTABLISHED
    provider: str | None = None
    mode: str | None = None
    reference_id: str | None = None
    consent_id: str | None = None
    verified_at: str | None = None
    verified_fields: list[str] = Field(default_factory=list)
    mismatched_fields: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    latency_ms: float | None = None

    @field_validator("verified_fields", "mismatched_fields")
    @classmethod
    def _names_only(cls, value: list[str]) -> list[str]:
        # A provider that returns values where names belong would put an
        # Aadhaar number into the case record. Anything that is not a plain
        # field name is dropped.
        return [v for v in (value or []) if isinstance(v, str) and _FIELD_NAME.match(v)]

    @property
    def issuer_verified(self) -> bool:
        return self.status is IssuerStatus.ISSUER_CONFIRMED

    def public(self) -> dict[str, Any]:
        """
        The published block. `status`, `issuer_verified` and `reason_codes`
        always; everything else only when it has a value -- the project's
        no-nulls convention for published blocks. A NOT_ESTABLISHED answer
        on every document, spelled out with six empty keys, grew a two-
        document response past its size guard. The audit record keeps
        every key.
        """
        block: dict[str, Any] = {
            "status": self.status.value,
            "issuer_verified": self.issuer_verified,
            "reason_codes": list(self.reason_codes),
        }
        for name in ("provider", "mode", "reference_id", "consent_id", "verified_at"):
            value = getattr(self, name)
            if value:
                block[name] = value
        if self.verified_fields:
            block["verified_fields"] = list(self.verified_fields)
        if self.mismatched_fields:
            block["mismatched_fields"] = list(self.mismatched_fields)
        return block


class IssuerVerificationProvider(ABC):
    """One trusted source for one or more document types."""

    #: Stable name, recorded in every result and audit entry.
    name: str = "provider"
    mode: ProviderMode = ProviderMode.ISSUER

    @abstractmethod
    def verify(self, request: IssuerVerificationRequest) -> IssuerVerificationResult:
        """Ask the source. Must not store the request's values."""


class NotConfiguredProvider(IssuerVerificationProvider):
    """
    The stub every type uses today. Always NOT_ESTABLISHED, never a network
    call, and says WHY: no provider configured for a type that could have
    one, or no trusted source existing at all for a type that cannot.
    """

    name = "none"

    def __init__(self, mode: ProviderMode = ProviderMode.ISSUER) -> None:
        self.mode = mode

    def verify(self, request: IssuerVerificationRequest) -> IssuerVerificationResult:
        code = NOT_AVAILABLE if self.mode is ProviderMode.NONE else NOT_CONFIGURED
        return IssuerVerificationResult(
            status=IssuerStatus.NOT_ESTABLISHED, provider=self.name,
            mode=self.mode.value, reason_codes=[code])


# ==========================================================================
# THE REGISTRY
# ==========================================================================

_REGISTERED: dict[str, IssuerVerificationProvider] = {}
_OVERRIDES: dict[str, IssuerVerificationProvider] = {}
_LOCK = threading.Lock()


def register_provider(provider: IssuerVerificationProvider) -> None:
    """Make a provider nameable in configuration."""
    with _LOCK:
        _REGISTERED[provider.name] = provider


def set_provider(document_type: str, provider: IssuerVerificationProvider | None) -> None:
    """Dependency injection for one type (tests, or a runtime wiring layer)."""
    key = str(document_type or "").upper()
    with _LOCK:
        if provider is None:
            _OVERRIDES.pop(key, None)
        else:
            _OVERRIDES[key] = provider


def reset_providers() -> None:
    with _LOCK:
        _OVERRIDES.clear()
    _config.cache_clear()


@lru_cache(maxsize=1)
def _config() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "config" / "issuer_verification.yaml"
    try:
        import yaml

        with path.open(encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        return loaded.get("issuer_verification") or {}
    except Exception as exc:  # a missing file means "nothing configured"
        logger.warning("Issuer verification configuration unavailable: %r", exc)
        return {}


def _entry(document_type: str) -> dict[str, Any]:
    return (_config().get("documents") or {}).get(str(document_type or "").upper()) or {}


def mode_for(document_type: str) -> ProviderMode:
    try:
        return ProviderMode(str(_entry(document_type).get("mode") or "none").lower())
    except ValueError:
        return ProviderMode.NONE


def route_for(document_type: str) -> str | None:
    return _entry(document_type).get("route")


def timeout_seconds() -> float:
    raw = os.getenv("ISSUER_VERIFICATION_TIMEOUT_SECONDS") or _config().get("timeout_seconds")
    try:
        return max(0.1, float(raw))
    except (TypeError, ValueError):
        return 10.0


def provider_for(document_type: str) -> IssuerVerificationProvider:
    """The provider for a type. Unknown or unregistered -> the stub."""
    key = str(document_type or "").upper()
    with _LOCK:
        if key in _OVERRIDES:
            return _OVERRIDES[key]
    mode = mode_for(key)
    name = str(_entry(key).get("provider") or "none").strip()
    if name == "none":
        return NotConfiguredProvider(mode)
    with _LOCK:
        registered = _REGISTERED.get(name)
    if registered is None:
        return _Unregistered(name, mode)
    return registered


class _Unregistered(IssuerVerificationProvider):
    """Configuration names a provider the code never registered."""

    def __init__(self, name: str, mode: ProviderMode) -> None:
        self.name = name
        self.mode = mode

    def verify(self, request: IssuerVerificationRequest) -> IssuerVerificationResult:
        return IssuerVerificationResult(
            status=IssuerStatus.NOT_ESTABLISHED, provider=self.name,
            mode=self.mode.value, reason_codes=[NOT_REGISTERED])


# ==========================================================================
# ASKING -- safely
# ==========================================================================

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4,
                                                  thread_name_prefix="issuer")


def verify(request: IssuerVerificationRequest) -> IssuerVerificationResult:
    """
    Ask the configured provider, bounded in time, and accept only a
    well-formed answer. Never raises, and never returns ISSUER_CONFIRMED
    for anything but a complete confirmation.
    """
    provider = provider_for(request.document_type)
    mode = getattr(provider, "mode", ProviderMode.ISSUER)
    mode_value = getattr(mode, "value", str(mode))
    started = time.perf_counter()

    def failed(code: str) -> IssuerVerificationResult:
        return IssuerVerificationResult(
            status=IssuerStatus.NOT_ESTABLISHED, provider=provider.name,
            mode=mode_value, consent_id=request.consent_id, reason_codes=[code],
            latency_ms=round((time.perf_counter() - started) * 1000, 2))

    try:
        future = _EXECUTOR.submit(provider.verify, request)
        result = future.result(timeout=timeout_seconds())
    except concurrent.futures.TimeoutError:
        logger.warning("Issuer provider %s timed out for %s", provider.name,
                       request.document_type)
        return failed(TIMEOUT)
    except Exception as exc:
        logger.warning("Issuer provider %s failed for %s: %s", provider.name,
                       request.document_type, type(exc).__name__)
        return failed(ERROR)

    if not isinstance(result, IssuerVerificationResult):
        return failed(ERROR)

    result.provider = result.provider or provider.name
    result.mode = result.mode or mode_value
    result.consent_id = result.consent_id or request.consent_id
    result.latency_ms = round((time.perf_counter() - started) * 1000, 2)

    # AN ANSWER THAT CANNOT BE TRACED IS NOT EVIDENCE.
    if result.status in (IssuerStatus.ISSUER_CONFIRMED, IssuerStatus.ISSUER_MISMATCH):
        if not (result.provider and result.provider != "none"
                and result.reference_id and result.verified_at):
            return failed(INCOMPLETE)
        if result.status is IssuerStatus.ISSUER_MISMATCH and MISMATCH not in result.reason_codes:
            result.reason_codes.insert(0, MISMATCH)
    return result


# ==========================================================================
# THE FLOW HOOK
# ==========================================================================

#: Verdicts the layer may evaluate: the document is structurally sound, or
#: held only for the authenticity evidence this layer supplies.
_EVALUATED = {"PASS"}


def _document_type(result: dict[str, Any]) -> str:
    document = result.get("document") or {}
    specialist = result.get("specialist") or {}
    return str(document.get("type") or result.get("type")
               or specialist.get("document_type") or "UNKNOWN").upper()


def _fields(result: dict[str, Any]) -> dict[str, Any]:
    fields = ((result.get("extraction") or {}).get("fields")) or {}
    flat: dict[str, Any] = {}
    for name, value in fields.items():
        if isinstance(value, dict) and "value" in value:
            value = value.get("value")
        flat[str(name)] = value
    return flat


def _held_only_for_authenticity(verification: dict[str, Any]) -> bool:
    return (str(verification.get("status") or "").upper() == "REVIEW"
            and list(verification.get("reason_codes") or []) == [REQUIRED_NOT_ESTABLISHED])


def apply(
    result: dict[str, Any],
    *,
    case_id: str | None = None,
    applicant_id: str | None = None,
    request_id: str | None = None,
    consent_id: str | None = None,
) -> dict[str, Any]:
    """
    Evaluate one processed document and record what the issuer said.

    CONFIRMED  may lift ONLY a hold placed for want of this evidence
               (REVIEW whose sole reason is AUTHENTICITY_NOT_ESTABLISHED).
    MISMATCH   fails the document, naming the fields that disagreed.
    NOT_ESTABLISHED  changes nothing unless the policy requires issuer
               evidence, in which case a PASS becomes REVIEW.

    The existing verdict is otherwise untouched: this layer never passes a
    document the deterministic verifiers did not.
    """
    from app.agents.verification import authenticity

    verification = result.get("verification")
    if not isinstance(verification, dict):
        return result

    from app.agents.los.response import verification_status

    document_type = _document_type(result)
    # Resolved the way the response resolves it: a specialist records its
    # verdict as `decision`, the Document Agent as `status`.
    status = str(verification_status(result) or "").upper()

    if status in _EVALUATED or _held_only_for_authenticity(verification):
        party_id = result.get("party_id")
        source_id = result.get("source_id")
        document_id = (f"{case_id}:{party_id}:{source_id}"
                       if case_id and party_id and source_id else None)
        answer = verify(IssuerVerificationRequest(
            document_type=document_type, fields=_fields(result), case_id=case_id,
            applicant_id=applicant_id, party_id=party_id, document_id=document_id,
            consent_id=consent_id, request_id=request_id))
        attempted = True
    else:
        answer = IssuerVerificationResult(
            status=IssuerStatus.NOT_ESTABLISHED, provider=None,
            mode=mode_for(document_type).value, consent_id=consent_id,
            reason_codes=[NOT_ATTEMPTED])
        document_id, attempted = None, False

    verification["issuer_verification"] = answer.public()
    verification["authenticity"] = answer.status.value
    verification["issuer_verified"] = answer.issuer_verified

    codes = list(verification.get("reason_codes") or [])
    if answer.status is IssuerStatus.ISSUER_MISMATCH:
        verification["status"] = "FAIL"
        for code in answer.reason_codes:
            if code not in codes:
                codes.append(code)
        verification["reason_codes"] = [c for c in codes if c != REQUIRED_NOT_ESTABLISHED]
        result["status"] = "REJECTED"
    elif answer.status is IssuerStatus.ISSUER_CONFIRMED:
        if _held_only_for_authenticity(verification):
            verification["status"] = "PASS"
            verification["reason_codes"] = []
            verification.pop("reasons", None)
            if str(result.get("status") or "").upper() == "REVIEW":
                result["status"] = "SUCCESS"
    elif authenticity.required() and status == "PASS":
        # Required and not established: a human looks. THE hold for every
        # document the LOS flow processes -- the agent defers its own here.
        verification["status"] = "REVIEW"
        if REQUIRED_NOT_ESTABLISHED not in codes:
            codes.append(REQUIRED_NOT_ESTABLISHED)
        verification["reason_codes"] = codes
        if str(result.get("status") or "").upper() == "SUCCESS":
            result["status"] = "REVIEW"

    _audit(answer, attempted=attempted, document_type=document_type,
           case_id=case_id, applicant_id=applicant_id, document_id=document_id,
           request_id=request_id, final_status=verification.get("status"))
    return result


# ==========================================================================
# AUDIT -- the risk-audit convention: JSONL, append-only, never raises
# ==========================================================================

_AUDIT_LOCK = threading.Lock()


def audit_enabled() -> bool:
    return os.getenv("ISSUER_VERIFICATION_AUDIT_ENABLED", "true").lower() == "true"


def audit_path() -> Path:
    return Path(os.getenv("ISSUER_VERIFICATION_AUDIT_PATH",
                          "./runtime/audit/issuer_verification.jsonl"))


def mask_aadhaar(value: Any) -> str | None:
    """Only the last four digits of an Aadhaar number are ever kept."""
    digits = re.sub(r"\D", "", str(value or ""))
    return f"XXXX-XXXX-{digits[-4:]}" if len(digits) >= 4 else None


def _audit(answer: IssuerVerificationResult, *, attempted: bool, document_type: str,
           case_id: str | None, applicant_id: str | None, document_id: str | None,
           request_id: str | None, final_status: Any) -> None:
    if not audit_enabled():
        return
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_id": uuid.uuid4().hex,
        "request_id": request_id,
        "case_id": case_id,
        "applicant_id": applicant_id,
        "document_id": document_id,
        "document_type": document_type,
        "attempted": attempted,
        "provider": answer.provider,
        "mode": answer.mode,
        "provider_status": answer.status.value,
        "reference_id": answer.reference_id,
        "consent_id": answer.consent_id,
        "verified_at": answer.verified_at,
        "verified_fields": list(answer.verified_fields),      # names only
        "mismatched_fields": list(answer.mismatched_fields),  # names only
        "reason_codes": list(answer.reason_codes),
        "latency_ms": answer.latency_ms,
        "document_verdict": final_status,
        "source": "los_flow",
    }
    try:
        target = audit_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Issuer verification audit write failed; the verdict was NOT affected.")


__all__ = [
    "IssuerStatus", "IssuerVerificationProvider", "IssuerVerificationRequest",
    "IssuerVerificationResult", "NotConfiguredProvider", "ProviderMode", "apply",
    "audit_path", "mask_aadhaar", "mode_for", "provider_for", "register_provider",
    "reset_providers", "route_for", "set_provider", "verify",
]
