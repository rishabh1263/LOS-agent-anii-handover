"""
The end-to-end LOS flow.

    uploads
      -> Document Agent (classify, verify, extract only when allowed)
      -> Financial Agent for financial documents
      -> normalised outputs
      -> KYC cross-check
      -> one response

Nothing here re-reads a document. The Document Agent already routes financial
uploads to the Financial Agent and returns normalised values, and KYC consumes
those values directly, so a page is rasterised once, recognised once and
classified once for the whole application.

Documents are independent of each other, so they are processed concurrently.
That parallelism is safe because each one offloads to the document executor and
hands recognition to the single serialised OCR worker -- RapidOCR is never
called from two threads at once. What actually overlaps is PDF rasterisation,
pypdf parsing and the financial parsers, which is where the wall-clock goes on
a multi-document application.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from app.agents.document_agent.workflow import process_document
from app.agents.los import config as los_config
from app.agents.los import overview, parties, response
from app.agents.kyc.agent import check_is_blocking, run_kyc
from app.agents.kyc.schemas import CheckStatus, KycRequest
from app.agents.los.mapping import to_kyc_source
from app.agents.los.summary import build_summary_async

logger = logging.getLogger(__name__)

# Worst-wins ordering across both vocabularies. The document statuses and the
# KYC statuses are different words for the same three outcomes, so they are
# ranked on one scale and the overall verdict is simply the worst.
_SEVERITY = {
    "SUCCESS": 0, "PASS": 0, "SKIPPED": 0,
    "PARTIAL": 1, "REVIEW": 1,
    "REJECTED": 2, "FAIL": 2,
    "FAILED": 3,
}

_PUBLIC = ("SUCCESS", "PARTIAL", "REVIEW", "REJECTED", "FAILED")


# Evidence that is NOT an identity document and must not go to the Document
# Agent's OCR pipeline. Each maps to a registered orchestration capability.
#
# Routing happens here so a client uploading a sale deed or a shop photo to
# /api/v1/los/process gets it assessed in the same call as everything else.
# Making them separate endpoints would mean the case is only complete if the
# client remembers to make three more requests.
_SPECIALIST_AGENTS: dict[str, str] = {
    "SALE_DEED": "sale_deed",
    "BUSINESS_PROOF_1": "business_evidence",
    "BUSINESS_PROOF_2": "business_evidence",
    "BANK_SIGNATURE": "signature_verification",
    "PAN_SIGNATURE": "signature_verification",
    "DRIVING_LICENSE_SIGNATURE": "signature_verification",
    "PASSPORT_SIGNATURE": "signature_verification",
    # A signature uploaded on its own, with no surrounding document.
    "SIGNATURE": "signature_verification",
    "STANDALONE_SIGNATURE": "signature_verification",
}

# Callers say "SIGNATURE"; the capability wants the specific input type.
_SIGNATURE_ALIASES = {"SIGNATURE": "STANDALONE_SIGNATURE"}


def specialist_for(expected_type: str | None) -> str | None:
    """Which capability owns this evidence type, if any."""
    return _SPECIALIST_AGENTS.get(str(expected_type or "").strip().upper())


def _rank(status: str | None) -> int:
    return _SEVERITY.get(str(status or "").upper(), 1)


def _public_status(rank: int) -> str:
    for name in _PUBLIC:
        if _SEVERITY[name] == rank:
            return name
    return "REVIEW"



# ---------------------------------------------------------------------------
# OPERATIONS
#
# PROCESS is the application-level verb and the canonical public operation:
# "run this applicant's documents through the whole flow". VERIFY and EXTRACT
# are the DOCUMENT AGENT's modes -- they decide whether that agent releases
# extracted fields -- and they reached the public API because this endpoint
# passed its `operation` straight through to it.
#
# They are kept accepted, because callers already send them, but they are now
# translated at this seam rather than being the vocabulary a client has to
# learn. Nothing else about the flow keys on the value: classification,
# specialist routing, KYC, cross-document checks, the decision and the
# summary all run identically for every operation. The ONLY thing it governs
# is whether the Document Agent hands back fields -- and that is still behind
# the verification gate either way.
# ---------------------------------------------------------------------------

#: The canonical public operation.
PROCESS = "PROCESS"

#: Everything /api/v1/los/process accepts, in the order a client should
#: prefer them.
PUBLIC_OPERATIONS = ("PROCESS", "EXTRACT", "VERIFY")

#: Public operation -> the Document Agent mode it runs as.
_DOCUMENT_MODE = {
    "PROCESS": "EXTRACT",
    "EXTRACT": "EXTRACT",
    "VERIFY": "VERIFY",
}


def document_mode(operation: str | None) -> str:
    """
    The Document Agent mode for a public operation.

    Raises ValueError, naming every accepted value, for anything else. The
    endpoint turns that into a 422 -- unknown operations are rejected, not
    quietly treated as the default.
    """
    value = (operation or PROCESS).strip().upper()

    mode = _DOCUMENT_MODE.get(value)
    if mode is None:
        raise ValueError(
            "operation must be one of: " + ", ".join(PUBLIC_OPERATIONS) + "."
        )

    return mode


class UploadedDocument:
    """
    One file offered to the flow, and whose it is.

    `party` is optional and defaults to None, so every existing caller
    keeps working unchanged: a document with no party is the primary
    applicant's, which is what a single-party case always meant. The flow
    stamps it before processing.
    """

    __slots__ = ("source_id", "filename", "content", "expected_type", "party")

    def __init__(
        self,
        source_id: str,
        filename: str,
        content: bytes,
        expected_type: str | None = None,
        party=None,
    ) -> None:
        self.source_id = source_id
        self.filename = filename
        self.content = content
        self.expected_type = expected_type
        self.party = party





def _is_type_mismatch(result: dict[str, Any]) -> bool:
    """Did this document fail only because it was not the type asked for?"""
    verification = result.get("verification") or {}
    return response.DOCUMENT_TYPE_MISMATCH in (
        verification.get("reason_codes") or []
    )


def _reported_values(check: Any) -> dict[str, str]:
    """
    What each document said for the field this check compared.

    Read off the evidence KYC already recorded -- these are normalised
    extracted values, the same ones already published under
    documents[].extraction. No OCR text, no candidate list, no scores.
    """
    values: dict[str, str] = {}

    for item in getattr(check, "evidence", None) or []:
        if item.source_id and item.value is not None:
            values[str(item.source_id)] = str(item.value)

    return values


def _disagreeing_sources(check: Any) -> list[str]:
    """
    Which uploads a failed KYC check is about.

    The response layer reports a conflict's `sources` so a reviewer knows
    WHICH two documents disagree; without them a six-document application
    reports NAME_MISMATCH against nothing in particular. The ids are read off
    the comparison KYC already performed -- the pairs it marked as not
    agreeing -- so nothing is inferred here.

    A check that failed before it could compare anything (an unreadable value,
    say) has no comparisons; it falls back to the sources that carried the
    field, which is what the check was about.
    """
    ordered: list[str] = []

    for comparison in getattr(check, "comparisons", None) or []:
        if comparison.agreed:
            continue
        for source_id in (comparison.left_source_id, comparison.right_source_id):
            if source_id and source_id not in ordered:
                ordered.append(source_id)

    if not ordered:
        for item in getattr(check, "evidence", None) or []:
            if item.source_id and item.source_id not in ordered:
                ordered.append(item.source_id)

    return ordered


def _classification_disabled(
    document: UploadedDocument,
    request_id: str,
) -> dict[str, Any]:
    """
    A document nobody was allowed to identify.

    The caller's expected_type is echoed under `hint` rather than as `type`,
    because it is what the caller ASSERTED, not what this service found. A
    hint promoted to a type would be a classification result nobody produced.
    """
    hint = str(document.expected_type or "").strip().upper() or None

    return {
        "request_id": f"{request_id}:{document.source_id}",
        "source_id": document.source_id,
        "status": "SKIPPED",
        "document": {
            "type": "UNKNOWN",
            "category": None,
            "supported": False,
            "hint": hint,
        },
        "extraction": None,
        "verification": {
            "status": "SKIPPED",
            "reason_codes": ["CLASSIFICATION_DISABLED"],
        },
        "evidence_refs": [],
        "processing": {},
        "errors": [
            {
                "code": "CLASSIFICATION_DISABLED",
                "message": (
                    "Classification is disabled by configuration; this "
                    "document was not identified, and no capability ran."
                ),
            }
        ],
    }


def _specialist_disabled(
    document: UploadedDocument,
    agent_id: str,
    request_id: str,
) -> dict[str, Any]:
    """A specialist that is switched off says so on the document it owns."""
    expected = str(document.expected_type or "").strip().upper()

    return {
        "request_id": f"{request_id}:{document.source_id}",
        "source_id": document.source_id,
        "status": "SKIPPED",
        "document": {
            "type": expected or "UNKNOWN",
            "category": agent_id,
            "supported": True,
        },
        "extraction": None,
        "verification": {
            "status": "SKIPPED",
            "reason_codes": ["CAPABILITY_DISABLED"],
        },
        "evidence_refs": [],
        "processing": {},
        "errors": [
            {
                "code": "CAPABILITY_DISABLED",
                "message": (
                    f"{agent_id} is disabled by configuration; this document "
                    "was not examined."
                ),
            }
        ],
    }


def _specialist_scores(
    expected: str | None, result: dict[str, Any],
) -> dict[str, Any]:
    """
    Score and confidence for a specialist's verdict, from its own checks.

    Empty when the capability reported no checks -- there is nothing to
    weigh, and a score computed from no evidence would be a number with
    no meaning behind it. Absent is the honest answer.

    Never raises: a capability must not fail to return because its result
    could not be scored.
    """
    try:
        from app.agents.verification import scoring

        checks = scoring.from_named(result.get("checks") or [])
        if not checks:
            return {}

        assessment = scoring.assess(
            str(expected or "SPECIALIST").upper(), checks,
        )
        published = assessment.public()
        # NUMBERS ONLY. The scoring layer also produces sentences, and
        # publishing them here was actively wrong: its checks are named
        # after the capability's internal steps, so a sale deed carrying
        # four precise codes -- PARTY_MISSING, CONSIDERATION_MISSING and
        # two more -- had all four sentences replaced by one generic
        # "verification checks could not be completed". The response
        # boundary derives the prose from the real reason codes instead.
        return {
            "verification_score": published["verification_score"],
            "verification_confidence": published["verification_confidence"],
        }
    except Exception:  # pragma: no cover - scoring must never break a call
        logger.exception("Specialist scoring failed")
        return {}


async def _run_specialist(
    document: UploadedDocument,
    agent_id: str,
    request_id: str,
) -> dict[str, Any]:
    """
    Run one upload through a specialist capability, via the orchestrator.

    Deliberately routed through run_agent rather than calling the service
    directly: the capability then gets the same config, retry, circuit
    breaker and bulkhead treatment as every other agent, and there is exactly
    one execution path to audit rather than one for orchestrated calls and a
    quieter one for the LOS flow.

    The bytes are written inside the upload sandbox because every specialist
    takes a path and every one of them refuses a path outside it.
    """
    from app.agents.document_verification.service import upload_root
    from app.orchestration.graph import run_agent

    source_request_id = f"{request_id}:{document.source_id}"
    expected = str(document.expected_type or "").strip().upper()

    root = upload_root()
    root.mkdir(parents=True, exist_ok=True)
    staged = root / f"los_{uuid.uuid4().hex}{Path(document.filename).suffix.lower()}"

    started = time.perf_counter()
    try:
        staged.write_bytes(document.content)

        payload: dict[str, Any] = {
            "file_path": str(staged),
            "source_id": document.source_id,
        }
        if agent_id == "business_evidence":
            payload["slot"] = expected
        elif agent_id == "signature_verification":
            payload["document_type"] = _SIGNATURE_ALIASES.get(expected, expected)

        state = await run_agent(
            agent_id=agent_id, payload=payload, request_id=source_request_id
        )

        result = state.get("result") or {}
        error = state.get("error")

        if error and not result:
            raise RuntimeError(str(error))

        elapsed = round((time.perf_counter() - started) * 1000, 2)

        return {
            "request_id": source_request_id,
            "source_id": document.source_id,
            # The specialist's own verdict becomes the document status, so the
            # application-level roll-up ranks it on the same scale as the rest.
            "status": result.get("decision") or "REVIEW",
            "document": {
                "type": expected,
                "category": agent_id,
                "supported": True,
            },
            "extraction": {"fields": result.get("fields") or {}},
            "verification": {
                "decision": result.get("decision"),
                "checks": result.get("checks") or [],
                "reason_codes": result.get("reason_codes") or [],
                # A specialist reported a verdict and its checks, and no
                # numbers -- a sale deed came back REVIEW with four reason
                # codes, `verification_score: null` and nothing to rank it
                # against other documents in the same queue.
                #
                # THE VERDICT IS UNTOUCHED. `decision` above is still the
                # capability's own, and the scoring module's status is
                # discarded: these are the two numbers that describe that
                # verdict, not a second opinion about it.
                **_specialist_scores(expected, result),
            },
            "evidence_refs": result.get("evidence_refs") or [],
            "specialist": result,
            # Three nested measurements of one call, not three costs:
            #
            #   specialist_ms    the whole thing, as the flow sees it
            #   mcp_ms           inside that, what MCP measured
            #   orchestration_ms specialist_ms minus mcp_ms -- routing,
            #                    config resolution, retry and breaker
            #
            # Reported separately so a slow request can be attributed, and
            # never added together.
            "processing": {
                "specialist_ms": elapsed,
                "mcp_ms": round(float(result.get("mcp_ms") or 0.0), 2),
                "orchestration_ms": round(
                    max(0.0, elapsed - float(result.get("mcp_ms") or 0.0)), 2
                ),
            },
            "errors": [],
        }

    except Exception as exc:
        logger.exception(
            "Specialist %s failed request_id=%s", agent_id, source_request_id
        )
        return {
            "request_id": source_request_id,
            "source_id": document.source_id,
            "status": "FAILED",
            "document": {
                "type": expected or "UNKNOWN",
                "category": agent_id,
                "supported": True,
            },
            "extraction": None,
            "verification": None,
            "evidence_refs": [],
            "processing": {
                "specialist_ms": round((time.perf_counter() - started) * 1000, 2)
            },
            "errors": [
                {
                    "code": "SPECIALIST_FAILED",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
        }
    finally:
        staged.unlink(missing_ok=True)


async def _process_one(
    document: UploadedDocument,
    operation: str,
    request_id: str,
    *,
    financial_analysis: bool = True,
) -> dict[str, Any]:
    """Run one upload through the Document Agent, never raising."""
    # Classification switched off.
    #
    # Nothing downstream may proceed as though the type were known. A
    # caller's expected_type is a HINT, not a classification result: routing
    # a document to a specialist on an unverified hint would let the hint
    # decide which capability judged it, and then report a verdict as though
    # the type had been established. So the document is reported SKIPPED with
    # the hint echoed, and no capability runs.
    if not los_config.classification_enabled():
        return _classification_disabled(document, request_id)

    agent_id = specialist_for(document.expected_type)
    if agent_id:
        if not los_config.specialist_enabled(agent_id):
            # Reported, not silently fallen through. Sending a shop photograph
            # to the Document Agent because its specialist is switched off
            # would classify it as an unreadable ID card, which looks like a
            # bad document rather than a disabled capability.
            return _specialist_disabled(document, agent_id, request_id)
        return _apply_verification_flag(
            await _run_specialist(document, agent_id, request_id)
        )

    try:
        result = await process_document(
            file_bytes=document.content,
            filename=document.filename,
            operation=operation,
            requested_class=document.expected_type,
            request_id=f"{request_id}:{document.source_id}",
            include_detail=False,
            include_signals=financial_analysis,
        )
    except ValueError as exc:
        # Rejected input (bad type, empty, oversized). One bad file must not
        # fail the whole application.
        result = {
            "request_id": f"{request_id}:{document.source_id}",
            "status": "FAILED",
            "document": {"type": "UNKNOWN", "category": None, "supported": False},
            "extraction": None,
            "verification": None,
            "kyc": None,
            "summary": "",
            "processing": {},
            "errors": [{"code": "INVALID_DOCUMENT", "message": str(exc)}],
        }
    except Exception as exc:
        logger.exception(
            "Document %s failed in the LOS flow request_id=%s",
            document.source_id, request_id,
        )
        result = {
            "request_id": f"{request_id}:{document.source_id}",
            "status": "FAILED",
            "document": {"type": "UNKNOWN", "category": None, "supported": False},
            "extraction": None,
            "verification": None,
            "kyc": None,
            "summary": "",
            "processing": {},
            "errors": [{
                "code": "DOCUMENT_PROCESSING_FAILED",
                "message": f"{type(exc).__name__}: {exc}",
            }],
        }

    result["source_id"] = document.source_id

    # What the CALLER asserted this file was. Carried so the response can show
    # the assertion beside the type actually found -- a mismatch is unreadable
    # without both halves.
    expected = str(document.expected_type or "").strip().upper()
    if expected and expected not in {"AUTO", "ANY"}:
        result["expected_type"] = expected

    # The per-document summary and kyc slot are redundant inside an
    # application-level response that carries both at the top.
    result.pop("summary", None)
    result.pop("kyc", None)
    return _apply_verification_flag(result)


def _apply_verification_flag(result: dict[str, Any]) -> dict[str, Any]:
    """
    Honour LOS_VERIFICATION_ENABLED on a finished document result.

    The flag was published in the LOS configuration snapshot and enforced
    nowhere: an operator who switched verification off still got a PASS and
    the extracted fields behind it, which is precisely what the gate exists
    to prevent. VERIFICATION_ENABLED -- the Verification Agent's own switch --
    did work; this one silently did not.

    Withheld AFTER the pipeline rather than before it. The verdict is
    discarded, not the classification: a caller still learns what the
    document is, which is the one thing verification being off does not put
    in doubt. Doing it earlier would mean threading the flag through the
    Document Agent, duplicating a switch that already lives with the agent
    that owns the decision.
    """
    if los_config.verification_enabled():
        return result

    # A document that could not be processed at all stays FAILED. Only the
    # verification verdict is being withheld here; downgrading an unreadable
    # file to SKIPPED would drop it out of the application roll-up entirely.
    if _rank(result.get("status")) <= _rank("SKIPPED"):
        result["status"] = "SKIPPED"

    result["extraction"] = None
    result["verification"] = {
        "status": "SKIPPED",
        "reason_codes": ["VERIFICATION_DISABLED"],
    }
    # A specialist's verdict is a verification verdict. Left in place it
    # would be read as one through the `specialist.decision` fallback, and
    # the document would report PASS with verification switched off.
    specialist = result.get("specialist")
    if specialist:
        specialist = dict(specialist)
        specialist["decision"] = "SKIPPED"
        specialist["reason_codes"] = ["VERIFICATION_DISABLED"]
        result["specialist"] = specialist

    return result


def _aggregate_timings(documents: list[dict[str, Any]]) -> dict[str, float]:
    """
    Roll per-document timings up.

    Stage costs are SUMMED because they are real work done, while the total is
    measured on the wall clock by the caller: documents run concurrently, so
    adding their totals would report more time than actually elapsed.
    """
    totals = {
        "ocr_ms": 0.0, "classification_ms": 0.0,
        "verification_ms": 0.0, "extraction_ms": 0.0,
    }
    for document in documents:
        processing = document.get("processing") or {}
        for key in totals:
            try:
                totals[key] += float(processing.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
    return {key: round(value, 2) for key, value in totals.items()}


def _match_profiles(
    *,
    documents: list[dict[str, Any]],
    primary,
    co_applicant,
    applicant_profile: dict[str, Any] | None,
    co_applicant_profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """
    One profile match per party, each over that party's own documents.

    ONE FUNCTION, BOTH PARTIES. The loop below is the whole of the
    co-applicant support: the same matcher, the same filter, a different
    party. A second path for the second person is a second place for the
    isolation to be got wrong.

    Never raises: profile matching is additional evidence, and a failure
    to produce it must not cost the caller the verdicts they asked for.
    """
    from app.agents.los import profile_match

    matches: list[dict[str, Any]] = []

    wanted = [(primary, applicant_profile)]
    if co_applicant is not None:
        wanted.append((co_applicant, co_applicant_profile))

    for party, supplied in wanted:
        try:
            profile = profile_match.merged(
                profile_match.from_request(**(supplied or {})),
                _stored_profile_for(party.party_id),
            )
            if profile.is_empty():
                # Nothing declared and nothing on file. There is no
                # profile to match, which is not the same as a profile
                # that failed to match.
                continue

            # THE ISOLATION BOUNDARY. Filtered before anything is
            # compared, so the matcher is never handed a mixed list.
            owned = _released_for_matching(parties.owned_by(
                documents, party.party_id,
                is_primary=party.party_role is parties.PartyRole.PRIMARY_APPLICANT,
            ))

            matches.append(profile_match.match_party(
                party_id=party.party_id,
                party_role=party.party_role.value,
                profile=profile,
                documents=owned,
            ).public())
        except Exception:  # pragma: no cover - evidence must not break a read
            logger.exception(
                "Profile matching failed for party %s", party.party_id)

    return matches


# ==========================================================================
# KYC, ONE PARTY AT A TIME
# ==========================================================================


def _kyc_for_party(
    documents: list[dict[str, Any]], *, party_id: str, request_id: str,
    party_role: str = "PRIMARY_APPLICANT",
) -> dict[str, Any]:
    """
    Cross-document KYC over ONE party's documents.

    THE ALGORITHM IS UNCHANGED. This builds the same sources from the
    same released fields and calls the same `run_kyc`; the only
    difference from before is that `documents` holds one person's
    uploads instead of the whole case's. Name matching, date and PAN
    normalisation, address comparison, the confidence model, the field
    weights, the thresholds and the reason codes are all whatever KYC
    already does.

    THE GATE IS OBEYED, AND IT WAS NOT BEFORE. `to_kyc_source` checks
    only that `extraction.fields` is populated -- and the internal
    envelope carries extraction whatever the verdict, because the gate
    is applied at the response boundary on the way out. So a REVIEW, a
    FAIL, a REJECTED and a run with extraction switched off ALL fed
    cross-document KYC with fields the caller was never shown, and the
    comment here claimed the opposite. Closed by running the documents
    through `_released_for_matching` -- the same gate Phase 4 profile
    matching uses, which is the same `response.released_extraction` the
    response boundary uses. One gate, three consumers.
    """
    sources = []
    for result in _released_for_matching(documents):
        source = to_kyc_source(result, result.get("source_id", "unknown"))
        if source is not None:
            sources.append(source)

    if not sources:
        # NOTHING TO CHECK, which is not the same as checked and found
        # wanting. `ran` says which: KYC itself also reports
        # INSUFFICIENT_SOURCES when it runs over a single document and
        # has nothing to compare it WITH, and the two must not be
        # confused -- one is a real REVIEW verdict about this party, the
        # other is the absence of a verdict. Internal; the public shape
        # is an allowlist that drops it.
        return {
            "party_id": party_id,
            "party_role": party_role,
            "ran": False,
            "status": CheckStatus.SKIPPED.value,
            "reason_codes": ["INSUFFICIENT_SOURCES"],
        }

    kyc_result = run_kyc(
        KycRequest(applicant_id=party_id, documents=sources),
        request_id=request_id,
    )

    return {
        # WHOSE VERDICT THIS IS. Internal only -- the public KYC object is
        # built from an explicit allowlist in `_public_envelope`, so this
        # never reaches a caller except as the `party_id` deliberately
        # added to case-level rows on a two-party case.
        "party_id": party_id,
        # WHOSE score this is, so the case-level roll-up can say which
        # person the number it published came from. `_case_kyc` takes the
        # MINIMUM of the parties' scores, and a minimum belongs to
        # somebody -- reporting it unattributed is what let a
        # co-applicant's name-match score be published as though it
        # described the case.
        "party_role": party_role,
        "ran": True,
        "status": kyc_result.status.value,
        "reason_codes": [code.value for code in kyc_result.reason_codes],
        # The FIELD-LEVEL view: one row per comparable field, with how
        # closely the values matched and how far that answer can be
        # relied on. Derived from the same checks below, never computed
        # a second time.
        "overall_score": kyc_result.overall_score,
        "overall_confidence": kyc_result.overall_confidence,
        "fields": [
            field.model_dump(mode="json") for field in kyc_result.fields
        ],
        # Per-check results, so a disagreement between documents can be
        # reported as a conflict rather than only as a status. The full
        # pair-comparison matrix stays internal.
        "checks": [
            {
                "check": check.check.value,
                "status": check.status.value,
                "reason_codes": [code.value for code in check.reason_codes],
                "source_ids": _disagreeing_sources(check),
                # What each document actually said. Without it a reviewer
                # is told two documents disagree and has to open both to
                # find out how.
                "values": _reported_values(check),
                # Whether this check failing may drive the verdict down
                # to FAIL, or is capped at REVIEW. Read from policy, not
                # decided here.
                "blocking": check_is_blocking(check.check.value),
            }
            for check in kyc_result.checks
        ],
    }


def _did_not_run(payload: dict[str, Any] | None) -> bool:
    """
    This party released nothing, as opposed to reaching a verdict.

    Keyed on an explicit flag, NOT on the reason codes: KYC reports
    INSUFFICIENT_SOURCES itself when it runs over a lone document and
    has nothing to compare it with, and reading that as "did not run"
    silently dropped a real REVIEW verdict out of the case roll-up.
    """
    return bool(payload) and not payload.get("ran", True)


def _case_kyc(
    per_party: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    """
    One case-level KYC verdict from the parties' own verdicts.

    NOT A CROSS-PARTY COMPARISON, AND IT MUST NEVER BECOME ONE. Nothing
    here compares a field from one party against a field from another.
    It rolls up verdicts that were each reached WITHIN one person's
    documents.

    A single-applicant case returns that party's result untouched, so
    the existing contract is byte-for-byte what it was.

    Worst-wins, matching the status semantics already used everywhere
    else in this response: a co-applicant whose documents disagree with
    each other still routes the case to a human. Score and confidence
    take the MINIMUM rather than an average, because averaging lets a
    well-documented applicant hide a poorly-documented co-applicant.

    A party whose KYC could not run contributes nothing -- neither a
    verdict nor a zero -- which is how "the co-applicant has not sent
    anything yet" avoids reading as "the co-applicant failed".
    """
    ran = [payload for payload in per_party if not _did_not_run(payload)]

    if not ran:
        # Nobody had anything comparable. Exactly the shape, and the
        # rank, a single-applicant case produced before.
        #
        # `ran` MUST be carried here. Without it `_did_not_run` read the
        # default and reported that KYC had run, so a VERIFY -- which
        # releases nothing by design -- stopped reporting KYC_NOT_RUN
        # and the caller was left to infer from a bare SKIPPED that the
        # gate had held rather than being told.
        return (
            {"ran": False,
             "status": CheckStatus.SKIPPED.value,
             "reason_codes": ["INSUFFICIENT_SOURCES"]},
            0,
        )

    if len(ran) == 1:
        payload = ran[0]
        return payload, _rank(payload.get("status"))

    reason_codes: list[str] = []
    for payload in ran:
        reason_codes.extend(payload.get("reason_codes") or [])

    # WHOSE SCORE THE MINIMUM IS.
    #
    # `overall_score` here is a MINIMUM over the parties, not a mean over
    # fields -- deliberately, so a well-documented applicant cannot hide
    # a poorly-documented co-applicant. But a minimum belongs to one
    # person, and publishing it unattributed let a co-applicant's
    # name-match score be read as a figure describing the whole case.
    # The party it came from is recorded here, at the only place that
    # knows.
    weakest = min(ran, key=lambda p: int(p.get("overall_score") or 0))

    aggregate = {
        "status": _worst_kyc_status(ran),
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "overall_score": int(weakest.get("overall_score") or 0),
        "overall_confidence": min(
            int(p.get("overall_confidence") or 0) for p in ran),
        "score_basis_party_id": weakest.get("party_id", ""),
        "score_basis_party_role": weakest.get(
            "party_role", "PRIMARY_APPLICANT"),
        # Each party's own tally, kept whole rather than summed: "three
        # of four checks passed" across two people says nothing about
        # either of them.
        "per_party": [
            {"party_id": p.get("party_id", ""),
             "party_role": p.get("party_role", "PRIMARY_APPLICANT"),
             "checks": p.get("checks") or []}
            for p in ran
        ],
        # WHOSE ROW IS WHOSE. Two parties produce two NAME rows, and a
        # reviewer reading a case-level list has to be able to tell them
        # apart. `party_id` is added only when the case actually has
        # more than one party, so a single-applicant response carries
        # exactly the keys it carried before.
        "fields": [
            {**field, "party_id": payload.get("party_id", "")}
            for payload in ran
            for field in (payload.get("fields") or [])
        ],
        "checks": [
            {**check, "party_id": payload.get("party_id", "")}
            for payload in ran
            for check in (payload.get("checks") or [])
        ],
    }

    return aggregate, _rank(aggregate["status"])


def _worst_kyc_status(payloads: list[dict[str, Any]]) -> str:
    """The most severe verdict any party reached, on the existing scale."""
    worst = max(payloads, key=lambda p: _rank(p.get("status")))
    return str(worst.get("status") or CheckStatus.SKIPPED.value)


def _status_for(
    documents: list[dict[str, Any]], kyc_rank: int,
) -> tuple[str, bool]:
    """
    The worst-wins roll-up over a set of documents and a KYC rank.

    Returns the status and whether NOTHING was verified, because the
    caller that owns the whole case also owes the client an error about
    that and a party section does not.

    EXTRACTED, NOT CHANGED. This was inline in `process_application` and
    computed the case verdict; it now also computes each party's, over
    that party's own documents and their own KYC. Same severity scale,
    same rules, same words -- one set of documents in, one status out.

    A DOCUMENT TYPE MISMATCH IS A WRONG UPLOAD, NOT A CREDIT REJECTION.
    The document itself fails -- verification FAIL, no extraction -- but
    the application is not rejected for it: the applicant sent the wrong
    file and can send the right one. Capped at REVIEW in the same way a
    non-blocking KYC disagreement is, so the case reaches a human with
    REQUEST_CORRECT_DOCUMENT rather than being turned down.
    """
    def _document_rank(result: dict[str, Any]) -> int:
        rank = _rank(result.get("status"))
        if _is_type_mismatch(result):
            return min(rank, _rank("REVIEW"))
        return rank

    # NOBODY SENT ANYTHING IS NOT A SUCCESS.
    #
    # A declared co-applicant who has not uploaded yet has no documents
    # and no KYC, and both of those rank as harmless -- so the roll-up
    # read SUCCESS for a party about whom NOTHING IS KNOWN. That is the
    # most misleading thing a per-party status could say, and it is the
    # reason this guard is explicit rather than left to the arithmetic.
    #
    # Unreachable at case level: the endpoint requires at least one file.
    if not documents:
        return "REVIEW", False

    worst = max(
        [_document_rank(result) for result in documents] + [kyc_rank],
        default=1,
    )

    # NOTHING VERIFIED IS NOT A SUCCESS EITHER.
    #
    # SKIPPED ranks alongside SUCCESS, which is right for one skipped
    # stage inside a healthy application and wrong where every document
    # was skipped: with verification switched off the documents all
    # report SKIPPED, KYC has no sources and also reports SKIPPED, and
    # the roll-up read SUCCESS for a case in which nothing was checked.
    #
    # A FLOOR, not an override -- something that actually failed still
    # reports FAILED rather than being softened to REVIEW.
    nothing_verified = not response.any_document_verified(documents)

    if nothing_verified:
        worst = max(worst, _rank("REVIEW"))

    status = _public_status(worst)

    # PARTIAL and REVIEW share a severity, and the roll-up renders that
    # severity as PARTIAL -- the right word where some of it worked.
    # Where nothing was verified, none of it did, so the word is REVIEW.
    # Only this case is renamed; every other PARTIAL keeps its name.
    if nothing_verified and status == "PARTIAL":
        status = "REVIEW"

    return status, nothing_verified


def _kyc_rank_of(payload: dict[str, Any] | None) -> int:
    """One party's KYC contribution to their roll-up."""
    if not payload or _did_not_run(payload):
        return 0
    return _rank(payload.get("status"))


def _public_risk(envelope: dict[str, Any]) -> dict[str, Any]:
    """The primary applicant's risk assessment."""
    risk = envelope.get("party_risk") or {}
    applicant_id = envelope.get("applicant_id")

    if applicant_id and applicant_id in risk:
        return risk[applicant_id]

    return next(iter(risk.values()), {})


def _public_eligibility(envelope: dict[str, Any]) -> dict[str, Any]:
    """
    The primary applicant's affordability verdict.

    Per party for the same reason income consistency is: a co-applicant
    has their own income, their own obligations and their own
    affordability, and assessing one against the other's figures is the
    two-party defect cross-document KYC already learned once.
    """
    eligibility = envelope.get("party_eligibility") or {}
    applicant_id = envelope.get("applicant_id")

    if applicant_id and applicant_id in eligibility:
        return eligibility[applicant_id]

    return next(iter(eligibility.values()), {})


def _public_income_consistency(envelope: dict[str, Any]) -> dict[str, Any]:
    """
    The primary applicant's income comparison.

    THE PRIMARY'S, AND SAID SO. A co-applicant's slip must never be
    compared against the applicant's statement -- that is the two-party
    defect cross-document KYC already learned -- so each party's
    comparison is computed over their own documents only. This response
    publishes the primary's; a co-applicant's is computed and held in
    the envelope, and publishing it belongs with the work that gives
    each party section its own income block.
    """
    income = envelope.get("party_income") or {}
    applicant_id = envelope.get("applicant_id")

    if applicant_id and applicant_id in income:
        return income[applicant_id]

    return next(iter(income.values()), {"status": "SKIPPED", "reason_codes": []})


def _public_cross_document(
    envelope: dict[str, Any], kyc: dict[str, Any],
) -> dict[str, Any]:
    """
    Agreement between documents, PER PARTY, in one list.

    WHAT THIS IS NOT, AND MUST NEVER BECOME. It is not a comparison
    between applicants. Two people on a joint application legitimately
    have different names, dates of birth and PANs; comparing them and
    reporting NAME_MISMATCH was a real defect, and the party scoping
    that fixed it is upstream of this function -- KYC runs once per
    party over that party's own documents, and `_case_kyc` rolls the
    verdicts up without ever comparing across them. This publishes what
    those runs found.

    WHY IT NO LONGER RETURNS AN EMPTY LIST ON A JOINT CASE. It used to:
    the checks were already published under each party, so repeating
    them here looked redundant, and an empty list was the conservative
    choice. It was the wrong one. A caller reading
    `cross_document: {status: SKIPPED, checks: []}` is told that nothing
    was cross-checked, when in fact every check had run and passed. That
    is not conservative, it is inaccurate -- and SKIPPED exists to mean
    "nothing was comparable", which is a different claim entirely. The
    rows now carry `party_id` (see `cross_document_from`), which is what
    makes publishing them honest rather than ambiguous.

    THE STATUS IS STILL EARNED, NOT ASSUMED. It is whatever the checks
    that actually ran came to, capped by policy, computed by the same
    `cross_document_from` a single-applicant case has always used. No
    check is invented and nothing is promoted to PASS merely because
    checks exist: a case whose every check was SKIPPED still reports
    SKIPPED.

    A single-applicant case is byte-for-byte unchanged -- no party_id is
    stamped, so the rows are exactly the rows it always published.
    """
    if not los_config.conflict_detection_enabled():
        return {"status": "SKIPPED", "checks": []}

    return response.cross_document_from(kyc)


def _party_sections(
    envelope: dict[str, Any], documents: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    `primary_applicant`, and `co_applicant` where there is one.

    PURELY A REGROUPING. `documents` are the compact dicts already
    published in the top-level `documents[]` and the profile matches are
    the ones Phase 4 computed. Nothing is re-verified, re-extracted or
    re-matched -- this call costs a dictionary lookup per document.

    ADDITIVE, SO EXISTING CALLERS ARE UNAFFECTED. Everything a caller
    reads today stays exactly where it was and says exactly what it said;
    these are new keys beside it. `co_applicant` is absent -- not null,
    not empty -- when the case has only one party, because a null there
    would read as "there is a second party and we do not know who".

    The primary applicant's section is always present, including on a
    single-applicant request, so a client has one shape to render rather
    than two code paths.
    """
    matches = {
        str(entry.get("party_id")): entry
        for entry in (envelope.get("profile_match") or [])
    }
    # Each party's OWN cross-document KYC, computed per party in the
    # flow. Never a cross-party comparison -- see `_case_kyc`.
    kyc_by_party = envelope.get("party_kyc") or {}

    # ON A SINGLE-APPLICANT CASE THE TOP-LEVEL `kyc` IS THIS PARTY'S.
    #
    # Publishing it again inside the section produced a byte-identical
    # copy carrying no information -- and it pushed a two-document
    # response past the size guard, which is what caught it. A party's
    # own `kyc` appears only where there is a second party to tell it
    # apart from.
    scoped_kyc = bool(envelope.get("co_applicant_id"))
    status_by_party = envelope.get("party_status") or {}

    applicant_id = str(envelope.get("applicant_id") or "")
    co_applicant_id = str(envelope.get("co_applicant_id") or "")

    if not applicant_id:
        return {}

    # Only the primary adopts an unstamped document -- see
    # `documents_of`. A pre-party stored row has no party_id and always
    # belonged to the case's applicant.
    own = {
        applicant_id: parties.owned_by(
            documents, applicant_id, is_primary=True),
    }
    if co_applicant_id:
        own[co_applicant_id] = parties.owned_by(
            documents, co_applicant_id, is_primary=False)

    sections: dict[str, Any] = {
        "primary_applicant": _section_for(
            applicant_id, "PRIMARY_APPLICANT", own[applicant_id],
            matches, kyc_by_party if scoped_kyc else {}, status_by_party),
    }

    if co_applicant_id:
        sections["co_applicant"] = _section_for(
            co_applicant_id, "CO_APPLICANT", own[co_applicant_id],
            matches, kyc_by_party, status_by_party)

    return sections


#: A party who sent nothing. NOT a verdict. Defined in `overview` and
#: re-exported here so the flow and the presentation layer cannot drift.
#:
#: A party with no documents used to report `status: REVIEW`, because the
#: roll-up over an empty document list finds nothing verified -- which is
#: true and completely misleading. A reviewer reading REVIEW beside an
#: empty `document_ids` concludes the party was assessed and found
#: wanting, when nobody has sent anything to assess.
#:
#: ABSENT INPUT, PROCESSING FAILURE, VERIFICATION FAILURE and KYC REVIEW
#: are four different states and this is the first of them. It is also
#: why KYC and the profile match are withheld here: publishing
#: `SKIPPED / INSUFFICIENT_SOURCES` would say a check ran and reached a
#: verdict, and none did.
NOT_PROVIDED = overview.NOT_PROVIDED


def _section_for(
    party_id: str,
    party_role: str,
    owned: list[dict[str, Any]],
    matches: dict[str, Any],
    kyc_by_party: dict[str, Any],
    status_by_party: dict[str, Any],
) -> dict[str, Any]:
    """One party's section, or the fact that they sent nothing."""
    if not owned:
        section = response.party_section(
            party_id=party_id, party_role=party_role, documents=[],
            status=NOT_PROVIDED,
        )
        # EXPLICITLY NULL, NOT ABSENT. A party who sent nothing has no
        # verification and no KYC, and a client rendering the section
        # should see that stated rather than have to infer it from two
        # missing keys.
        section["verification"] = None
        section["kyc"] = None
        return section

    section = response.party_section(
        party_id=party_id,
        party_role=party_role,
        documents=owned,
        profile_match=matches.get(party_id),
        kyc=kyc_by_party.get(party_id),
        status=status_by_party.get(party_id),
    )

    # HOW THIS PARTY'S DOCUMENTS CAME OUT, as an object beside the
    # counts. `verification_summary` stays exactly as it was -- this is
    # the same verdicts read a second way, for a reader who wants the
    # status and the reasons rather than a tally.
    section["verification"] = overview.verification_of(owned)

    return section


def _released_for_matching(
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Each document reduced to the fields the verification gate RELEASED.

    THE DEFECT THIS CLOSES. Profile matching runs on the internal
    envelope, which carries `extraction` on every document regardless of
    verdict -- the gate is applied at the response boundary, on the way
    out. Handing the matcher the internal list therefore compared a
    declared PAN against fields a REVIEW had withheld from the caller,
    and reported PASS at score 100 on evidence nobody was allowed to see.
    The same hole let a deployment with extraction switched off keep
    matching on the fields it had just been told not to release.

    ONE GATE, NOT TWO. `released_extraction` is the same function the
    response boundary calls. Re-deciding "was this releasable" here is
    how the two answers drift, which is exactly how the gate was lost
    once before in the compact response shape.
    """
    from app.agents.los.response import released_extraction, verification_status

    released: list[dict[str, Any]] = []

    for document in documents:
        allowed = released_extraction(
            document.get("extraction"), verification_status(document))
        if not allowed or not allowed.get("fields"):
            continue
        # Field quality travels with the fields it describes; it feeds the
        # confidence figure and is never published.
        released.append({
            **document,
            "extraction": {
                "fields": allowed["fields"],
                "field_quality": (document.get("extraction") or {}).get(
                    "field_quality") or {},
            },
        })

    return released


def _income_consistency_for(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Whether one party's statement supports the salary their slip states.

    A SEPARATE CHECK FROM IDENTITY KYC, deliberately. Name, date of
    birth, PAN, father's name and address decide whether the documents
    describe one person. This decides whether they tell one story about
    income, which is a different question with a different policy file
    and its own reason codes. It contributes nothing to the KYC verdict
    and nothing to the case decision -- it is a signal a reviewer reads.

    THE SAME GATE, A FOURTH CONSUMER. `_released_for_matching` is what
    profile matching and cross-document KYC already use, so a figure
    withheld from the caller is withheld from this comparison too.
    """
    from app.agents.income import consistency

    bank_evidence = None
    salary_slip = None

    for result in _released_for_matching(documents):
        document_type = str(
            (result.get("document") or {}).get("type") or "").upper()
        fields = (result.get("extraction") or {}).get("fields") or {}

        if document_type == "BANK_STATEMENT" and bank_evidence is None:
            bank_evidence = fields.get("income_evidence")
        elif document_type == "SALARY_SLIP" and salary_slip is None:
            signals = fields.get("signals") or {}
            detail = fields.get("detail") or {}
            # THE SLIP'S OWN FIGURES, under the names this project
            # already gives them. `net_pay` and `gross_earnings` reach
            # the response through `signals`, where the financial layer
            # normalises every document's income fields into one shape.
            salary_slip = {
                "net_pay": signals.get("monthly_net_salary"),
                "gross_earnings": signals.get("monthly_gross_salary"),
                "pay_period": detail.get("pay_period"),
            }
            if not any(salary_slip.values()):
                salary_slip = {}

    return consistency.check(
        bank_evidence=bank_evidence, salary_slip=salary_slip)


def _eligibility_inputs(
    income: dict[str, Any], application: Any,
) -> "EligibilityInputs":
    """
    What affordability is assessed from, assembled and nothing more.

    THE INCOME IS THE INCOME PIPELINE'S, NOT A NEW ONE. It comes out of
    the income consistency result, which itself read only figures the
    released-extraction gate allowed and the income policy accepted. The
    salary slip's stated net pay is preferred where there is one: an
    employer stating a salary is a stronger claim than credits nobody
    labelled, and where only the credits exist the provenance says so.

    THE LOAN TERMS ARE THE APPLICATION'S. Absent ones stay absent --
    parsing is the engine's job and a missing tenure is a missing tenure.
    """
    from app.agents.eligibility.schemas import (
        EligibilityInputs, IncomeSource, ObligationsSource, PropertyValueSource,
    )

    slip = (income or {}).get("salary_slip") or {}
    bank = (income or {}).get("bank_statement") or {}

    monthly_income = None
    source = IncomeSource.NONE

    if slip.get("amount"):
        monthly_income = slip["amount"]
        source = (IncomeSource.SALARY_SLIP_GROSS
                  if "GROSS" in str(slip.get("figure") or "")
                  else IncomeSource.SALARY_SLIP_NET)
    elif bank.get("estimated_monthly_amount"):
        monthly_income = bank["estimated_monthly_amount"]
        source = IncomeSource.BANK_RECURRING_CREDIT

    # DECLARED OBLIGATIONS ARE PASSED AS DECLARED, and whether a declared
    # figure may be used at all is decided by eligibility_policy.yaml,
    # which does not accept one by default. Capturing a number and using
    # a number are separate decisions and this is only the first.
    declared = getattr(application, "declared_monthly_obligations", None)
    # `application` is None on a case's first pass; getattr covers it and
    # every field below reads as absent, which is what it is.

    return EligibilityInputs(
        monthly_income=_as_number(monthly_income),
        income_source=source,
        income_consistency_status=(income or {}).get("status"),
        monthly_obligations=_as_number(declared),
        obligations_source=(ObligationsSource.DECLARED if declared
                            else ObligationsSource.NONE),
        loan_amount=_as_number(getattr(application, "loan_amount", None)),
        tenure_months=_as_int(getattr(application, "tenure_months", None)),
        interest_rate_pct=_as_number(
            getattr(application, "interest_rate_pct", None)),
        product=getattr(application, "product", None),
        # Captured on the application at intake; the policy decides
        # whether it is a criterion at all.
        employment_type=getattr(application, "employment_type", None),
        # Declared at intake; the policy decides whether LTV applies at all
        # and whether a declared value is an accepted source.
        property_value=_as_number(getattr(application, "property_value", None)),
        property_value_source=(
            PropertyValueSource.DECLARED
            if getattr(application, "property_value", None)
            else PropertyValueSource.NONE),
    )


def _as_number(value: Any) -> float | None:
    """A captured figure as a number, or nothing. Never a default."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_number(value)
    return int(number) if number is not None else None


async def _eligibility_for(
    income: dict[str, Any], case_id: str | None,
) -> dict[str, Any] | None:
    """
    One party's affordability verdict, through the registered agent.

    ROUTED THROUGH `run_agent` RATHER THAN CALLED DIRECTLY, the same way
    the specialist capabilities are: the agent is then configurable,
    disableable and audited exactly like every other one, and an
    eligibility verdict produced inside a document pipeline is the same
    verdict as one produced by calling the agent on its own.

    ASSESSED EVEN WITH NO APPLICATION RECORD, and this is deliberate.
    `/api/v1/los/process` writes the application row AFTER the pipeline
    runs, so on a case's first pass there is nothing to read -- and
    returning None there made the stage vanish from that response
    entirely, while the same case processed a second time reported it.
    One case, two different answers, decided by whether anyone had
    uploaded to it before.

    An absent application means absent loan terms, which the engine
    already handles: it reports LOAN_AMOUNT_MISSING and
    EMI_INPUTS_MISSING and assesses nothing. That is the truth about
    the case, and it is available on the first pass as well as the
    second.
    """
    from app.orchestration.graph import run_agent

    application = None
    if case_id:
        try:
            from app.store import get_repository

            application = get_repository().get_application(case_id)
        except Exception:
            application = None

    inputs = _eligibility_inputs(income, application)

    state = await run_agent(
        agent_id="eligibility_agent",
        payload=inputs.model_dump(mode="json"),
        request_id=f"elig_{case_id}",
    )
    result = (state or {}).get("result") or {}
    return result.get("eligibility")


def _risk_for(
    eligibility: dict[str, Any], case_id: str | None,
) -> dict[str, Any] | None:
    """
    The risk assessment, reading the affordability verdict just produced.

    THE FOIR IS NOT RECOMPUTED HERE. It is handed over in
    `eligibility`, and the FOIR rule bands the published percentage
    instead of assembling its own inputs. One applicant, one FOIR,
    whichever response a reviewer is reading.

    DETERMINISTIC ENGINE ONLY on this path (`use_llm=False`). The risk
    agent's summary is a model call, and a document pipeline that waits
    on one to return a document verdict has made the model part of the
    critical path. The narrated summary remains available on the agent's
    own route, where a caller asked for it.

    NEVER RAISES, and never changes a verdict: the result is published
    beside the others and read by nothing that decides anything.
    """
    if not eligibility:
        return None

    try:
        from app.agents.fraud_risk.agent import FraudRiskAgent
        from app.agents.fraud_risk.config import enabled as risk_enabled
        from app.agents.fraud_risk.schemas import FraudRiskRequest

        if not risk_enabled():
            return None

        inputs = eligibility.get("inputs") or {}
        # THE CASE IS NAMED ONCE, AT THE TOP OF THE RESPONSE.
        #
        # `application_id` echoed the case id inside the risk block, and
        # the same request against two cases then differed in a field
        # that says nothing a reader did not already have. The risk
        # agent's own route still sets it from what its caller sent.
        request = FraudRiskRequest(
            eligibility={
                "status": eligibility.get("status"),
                "foir_pct": (eligibility.get("foir") or {}).get("value_pct"),
                "ltv_pct": (eligibility.get("ltv") or {}).get("value_pct"),
                "proposed_emi": inputs.get("proposed_emi"),
                "income_used": inputs.get("monthly_income"),
                "reason_codes": list(eligibility.get("reason_codes") or []),
            },
        )
        response = FraudRiskAgent(use_llm=False).assess(request)

        # THE COMPACT VIEW, AND THE AGENT'S OWN RULE FOR IT: "never drop
        # audit data to make a payload smaller -- only stop returning
        # it." `assess` has already written the full record, data gaps
        # and all, to the risk audit trail. What a document response
        # needs is the verdict and which rules fired; on an intake-grade
        # case the full record was 2.5 KB, nearly half of it a list of
        # rules that had nothing to evaluate.
        #
        # The FOIR figure is not lost: it is published once, in
        # `eligibility.metrics`, which is where it was computed.
        from app.agents.fraud_risk.schemas import FraudRiskCompactResponse

        return FraudRiskCompactResponse.from_full(response).model_dump(
            mode="json")
    except Exception as exc:
        logger.warning("Risk assessment unavailable for %s: %r", case_id, exc)
        return None


def _stored_profile_for(party_id: str):
    """
    The persisted profile for one party, where the store has one.

    Request values win over these -- see `profile_match.merged`. Reading
    the store is best-effort: an unavailable case store must not turn a
    successful extraction into a failure.
    """
    from app.agents.los import profile_match

    try:
        from app.store import get_repository

        return profile_match.stored_profile(
            get_repository().get_applicant(party_id))
    except Exception:
        return profile_match.Profile()


async def process_application(
    uploads: Iterable[UploadedDocument],
    *,
    operation: str = PROCESS,
    applicant_id: str | None = None,
    case_id: str | None = None,
    request_id: str,
    # -- the optional second party on this case -------------------------
    #
    # Both default to absent, so a single-applicant call behaves exactly
    # as it did before co-applicants existed.
    co_applicant_id: str | None = None,
    co_applicant_uploads: Iterable[UploadedDocument] | None = None,
    # -- declared profiles, matched against each party's OWN documents ---
    #
    # Both optional. A call that supplies neither produces no profile
    # match at all, which is what every existing caller gets.
    applicant_profile: dict[str, Any] | None = None,
    co_applicant_profile: dict[str, Any] | None = None,
    profile_matching: bool = True,
    use_llm_summary: bool | None = None,
    cross_document_checks: bool = True,
    summarise: bool = True,
    financial_analysis: bool = True,
) -> dict[str, Any]:
    """
    Run every document, cross-check them, and return one response.

    STAGE BOUNDARY. `cross_document_checks` and `summarise` exist so a caller
    can run the part of this pipeline its stage owns and no more.

    The FOS stage owns classification and BASIC DOCUMENT VERIFICATION: is this
    upload usable as the type it claims to be? It does NOT own KYC, which
    asks a different question -- do the identity fields across several
    documents describe one person? -- and belongs after the CPA handoff.

    Calling the whole pipeline from a FOS upload ran KYC every time a
    document was added, so an officer uploading a PAN and a licence got a
    cross-document name and date-of-birth comparison they had not asked for,
    at a stage that has no authority to act on it. It also paid for a summary
    nobody read.

    Both default to True, so the LOS endpoint behaves exactly as before.
    """

    started = time.perf_counter()
    uploads = list(uploads)

    # PROCESS is the public verb; the Document Agent is handed its own mode.
    operation = document_mode(operation)

    # One applicant, many cases.
    #
    # An applicant_id identifies a PERSON and is reused across every
    # application they ever make; a case_id identifies ONE of those
    # applications. Conflating them would merge a rejected application from
    # last year with a fresh one, and a name mismatch between the two would
    # be reported as a conflict inside the new case.
    #
    # Omitting case_id starts a new case. Supplying one continues it. This
    # call is stateless -- everything in the response is scoped to the case
    # named in it -- so "continuing" means the client keeps sending the same
    # id, and nothing from another case can reach this one.
    case_id = (case_id or "").strip() or f"case_{uuid.uuid4().hex}"

    # ---------------------------------------------------------------
    # WHO IS ON THIS CASE.
    #
    # Resolved once, before anything is processed, so every document is
    # stamped with its owner at the point it enters the pipeline rather
    # than being attributed afterwards from whichever list it came out of.
    # Attribution after the fact is how a co-applicant's PAN ends up on
    # the primary applicant's file.
    # ---------------------------------------------------------------
    primary, co_applicant = parties.resolve(
        case_id=case_id,
        applicant_id=applicant_id,
        co_applicant_id=co_applicant_id,
    )
    applicant_id = primary.party_id

    co_uploads = list(co_applicant_uploads or [])
    if co_uploads and co_applicant is None:
        raise ValueError(
            "co_applicant_files were supplied without a co_applicant_id. "
            "A document with no owner cannot be filed against a case."
        )

    for upload in uploads:
        upload.party = primary
    for upload in co_uploads:
        upload.party = co_applicant

    # ONE PIPELINE, TWO PARTIES. Both sets go through exactly the same
    # `_process_one`; only the party stamped on each upload differs. A
    # second code path for the co-applicant is a second place for the
    # verification gate to be got wrong.
    every_upload = list(uploads) + co_uploads

    # Independent work, run together.
    documents = await asyncio.gather(
        *(_process_one(upload, operation, request_id,
                       financial_analysis=financial_analysis)
          for upload in every_upload)
    )

    # OWNERSHIP IS STAMPED FROM THE UPLOAD, NOT INFERRED FROM THE RESULT.
    #
    # `_process_one` has several exits -- specialist, identity, financial,
    # disabled, failed -- and requiring each of them to remember the party
    # would mean one of them eventually forgetting. Zipped against the
    # uploads in the order they were submitted, which `asyncio.gather`
    # preserves, so a document cannot be attributed to the wrong person by
    # a route that did not think about it.
    for upload, result in zip(every_upload, documents):
        if upload.party is not None:
            result["party_id"] = upload.party.party_id
            result["party_role"] = upload.party.party_role.value

    # ---------------------------------------------------------------
    # KYC. Consumes only what the agents already released: a document
    # whose fields were withheld by the verification gate contributes
    # nothing, which is the gate doing its job.
    # ---------------------------------------------------------------
    kyc_started = time.perf_counter()
    errors: list[dict[str, Any]] = []

    # PARTY-SCOPED, BECAUSE OF WHAT KYC ASKS.
    #
    # KYC asks whether several documents describe ONE person, so it may
    # only ever compare documents belonging to the same person. Run over
    # a whole two-party case it compared the primary applicant's PAN
    # against the co-applicant's and reported NAME_MISMATCH,
    # DOB_MISMATCH, PAN_MISMATCH and FATHER_NAME_MISMATCH -- two
    # different people correctly disagreeing, read as a KYC failure, and
    # a clean joint application routed to a human for it.
    #
    # NOTHING ABOUT THE COMPARISON CHANGES. Same matchers, same
    # thresholds, same weights, same confidence model, same reason
    # codes. Only the SCOPE of the input changes, which is why a
    # single-applicant case runs exactly the call it ran before: the
    # primary's document set is the whole case's.
    kyc_parties = [
        # Only the primary adopts an unstamped document -- a row written
        # before parties existed always belonged to the case's applicant.
        (primary, parties.owned_by(
            documents, primary.party_id, is_primary=True)),
    ]
    if co_applicant is not None:
        kyc_parties.append(
            (co_applicant, parties.owned_by(
                documents, co_applicant.party_id, is_primary=False)))

    party_kyc: dict[str, dict[str, Any]] = {}

    if not cross_document_checks:
        # The caller's stage does not own KYC. Nothing is computed and
        # nothing is claimed: `kyc` is absent from the envelope rather than
        # reported as SKIPPED, because SKIPPED is a KYC verdict and this is
        # the absence of one.
        kyc_payload = None
        kyc_rank = 0
    elif not los_config.kyc_enabled():
        kyc_payload = {
            "status": CheckStatus.SKIPPED.value,
            "reason_codes": ["KYC_DISABLED"],
            "checks": [],
        }
        kyc_rank = _rank(CheckStatus.SKIPPED.value)
    else:
        for party, owned in kyc_parties:
            party_kyc[party.party_id] = _kyc_for_party(
                owned, party_id=party.party_id, request_id=request_id,
                party_role=party.party_role.value)

        kyc_payload, kyc_rank = _case_kyc(
            [party_kyc[party.party_id] for party, _ in kyc_parties])

        if _did_not_run(kyc_payload):
            errors.append({
                "code": "KYC_NOT_RUN",
                "message": (
                    "No document released extracted fields, so there was "
                    "nothing to cross-check. VERIFY never releases fields, "
                    "and EXTRACT releases them only after verification "
                    "passes."
                ),
            })

    kyc_ms = (time.perf_counter() - kyc_started) * 1000

    # EACH PARTY'S OWN DOCUMENT/KYC STATE.
    #
    # The same roll-up the case uses, over that party's own documents
    # and their own KYC. Nothing is re-verified and no KYC is re-run --
    # both results are already in hand; this is arithmetic over them.
    party_status = {
        party.party_id: _status_for(
            owned, _kyc_rank_of(party_kyc.get(party.party_id)))[0]
        for party, owned in kyc_parties
    }

    # INCOME CONSISTENCY, PER PARTY.
    #
    # Under the same `cross_document_checks` guard as KYC, because it is
    # the same kind of claim: a statement about how two documents relate
    # to each other, which the FOS stage has no authority to make. A FOS
    # upload produces no income comparison at all rather than a SKIPPED
    # one -- SKIPPED is a verdict, and not running is not.
    #
    # IT CHANGES NO VERDICT. Nothing below reads it: `status`, the
    # decision and `next_action` are computed exactly as they were. It
    # travels to the response as a signal and stops there.
    party_income: dict[str, dict[str, Any]] = {}
    if cross_document_checks:
        for party, owned in kyc_parties:
            party_income[party.party_id] = _income_consistency_for(owned)

    # AFFORDABILITY, PER PARTY, AFTER THE EVIDENCE IT READS.
    #
    # Under the same `cross_document_checks` guard as KYC and income: a
    # FOS upload verifies documents and has no authority to say anything
    # about whether a loan is affordable.
    #
    # IT CHANGES NO VERDICT. Nothing below reads it -- `status`, the
    # decision and `next_action` are computed exactly as they were. It
    # travels to the response as a stage result and stops there, because
    # deciding a loan weighs risk, RCU and a human, none of which this
    # stage can see.
    party_eligibility: dict[str, dict[str, Any]] = {}
    party_risk: dict[str, dict[str, Any]] = {}
    if cross_document_checks and party_income:
        for party, _owned in kyc_parties:
            try:
                verdict = await _eligibility_for(
                    party_income.get(party.party_id) or {}, case_id)
            except Exception as exc:
                # An affordability failure must not take a document
                # pipeline down with it.
                logger.warning("Eligibility unavailable for %s: %r",
                               party.party_id, exc)
                verdict = None
            if verdict:
                party_eligibility[party.party_id] = verdict
                risk = _risk_for(verdict, case_id)
                if risk:
                    party_risk[party.party_id] = risk

    # ---------------------------------------------------------------
    # Overall verdict: the worst of every document and the KYC result.
    # ---------------------------------------------------------------
    # A DOCUMENT TYPE MISMATCH IS A WRONG UPLOAD, NOT A CREDIT REJECTION.
    #
    # The document itself fails -- verification FAIL, no extraction -- but
    # the application is not rejected for it: the applicant sent the wrong
    # file and can send the right one. Capped at REVIEW for the roll-up in
    # the same way a non-blocking KYC disagreement is, so the case reaches a
    # human with REQUEST_CORRECT_DOCUMENT rather than being turned down.
    status, nothing_verified = _status_for(documents, kyc_rank)

    if nothing_verified:
        errors.append({
            "code": response.NO_VERIFIED_DOCUMENTS,
            "message": (
                "No document cleared verification, so no verified evidence "
                "supports this application. Extraction was withheld for "
                "every document and KYC had nothing to cross-check."
            ),
        })

    for result in documents:
        for error in result.get("errors") or []:
            errors.append({**error, "source_id": result.get("source_id")})

    # ---------------------------------------------------------------
    # PROFILE MATCHING, per party, over that party's OWN documents.
    #
    # An ADDITIONAL evidence layer. It runs after every verdict is final
    # and cannot reach back into one: nothing below writes to a
    # document's status, reason codes or score. A profile mismatch is
    # reported as profile evidence, and the document remains whatever
    # verification found it to be.
    #
    # Deterministic and free of I/O: it reads fields the gate already
    # released. No OCR, no model call, no second pass over any document.
    # ---------------------------------------------------------------
    profile_started = time.perf_counter()
    profile_matches: list[dict[str, Any]] = []

    if profile_matching:
        profile_matches = _match_profiles(
            documents=documents,
            primary=primary,
            co_applicant=co_applicant,
            applicant_profile=applicant_profile,
            co_applicant_profile=co_applicant_profile,
        )
    profile_ms = round((time.perf_counter() - profile_started) * 1000, 2)

    processing = _aggregate_timings(documents)
    processing["profile_match_ms"] = profile_ms
    processing["kyc_ms"] = round(kyc_ms, 2)
    processing["total_ms"] = round((time.perf_counter() - started) * 1000, 2)

    envelope: dict[str, Any] = {
        "request_id": request_id,
        "applicant_id": applicant_id,
        "case_id": case_id,
        # Present only when the case actually has a second party. A null
        # `co_applicant_id` on every single-applicant response would read
        # as "there is one and we do not know it".
        **({"co_applicant_id": co_applicant.party_id}
           if co_applicant is not None else {}),
        "status": status,
        "documents": documents,
        # Absent when nothing was matched, so a caller that supplied no
        # profile sees no empty section suggesting one was attempted.
        **({"profile_match": profile_matches} if profile_matches else {}),
        # Per-party KYC, so each section can publish its own. Internal:
        # the public shape is built by `_party_sections` through the same
        # allowlist the case-level object uses.
        **({"party_kyc": party_kyc} if party_kyc else {}),
        # Each party's own roll-up, published on their section.
        **({"party_status": party_status} if party_status else {}),
        # Income consistency, per party. Internal: the response
        # publishes the primary applicant's.
        **({"party_income": party_income} if party_income else {}),
        # Affordability, per party. Same arrangement.
        **({"party_eligibility": party_eligibility}
           if party_eligibility else {}),
        **({"party_risk": party_risk} if party_risk else {}),
        "summary": "",
        "processing": processing,
        "errors": errors,
    }

    # Present only when this stage actually ran it. A `kyc` key carrying
    # SKIPPED and one carrying nothing say different things, and a FOS
    # upload has no KYC verdict of any kind to report.
    if kyc_payload is not None:
        envelope["kyc"] = kyc_payload

    # Awaited, not called inline: with the model switched on this is a network
    # call, and the flow is already on the event loop.
    #
    # The summary is written LAST, from results that are already final, and it
    # cannot change any of them. A model failure costs the sentence and
    # nothing else -- build_summary_async falls back to the deterministic
    # summary rather than propagating.
    if use_llm_summary is None:
        use_llm_summary = los_config.llm_summary_enabled()

    summary_started = time.perf_counter()
    if summarise:
        envelope["summary"], envelope["summary_source"] = (
            await build_summary_async(envelope, use_llm=use_llm_summary)
        )
    else:
        # The caller builds its own answer from these results. Writing a
        # second one here costs a model call on every upload and is read by
        # nobody -- FOS never looks at `summary`.
        envelope["summary"] = ""
        envelope["summary_source"] = "not_requested"
    summary_ms = round((time.perf_counter() - summary_started) * 1000, 2)

    # Attributed separately: `summary_ms` is what the stage cost, `llm_ms` is
    # how much of that was the model. They differ when the model was not
    # consulted at all, and that difference is the whole point of the
    # reachability probe.
    processing["summary_ms"] = summary_ms
    processing["llm_ms"] = (
        summary_ms if envelope.get("summary_source") == "llm" else 0.0
    )
    processing["total_ms"] = round((time.perf_counter() - started) * 1000, 2)

    # The stage breakdown, to the log rather than to the client.
    #
    # It is deliberately absent from the response -- ocr_ms and friends
    # describe how this service is built, and a client reading them would
    # turn an implementation detail into a contract. But it has to go
    # SOMEWHERE, or nobody tuning the service can see which stage cost what,
    # and the only remaining answer is "add a timer and redeploy".
    #
    # One line per request, correlatable by request_id, and carrying nothing
    # but numbers: no field values, no filenames, no applicant data.
    logger.info(
        "los timings request_id=%s documents=%d source=%s %s",
        request_id,
        len(documents),
        envelope.get("summary_source"),
        " ".join(f"{key}={value}" for key, value in sorted(processing.items())),
    )

    return _public_envelope(envelope)


def _public_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """
    Reshape the internal result into the client-facing contract.

    Kept as a final pass rather than threaded through every producer: the
    internal dicts stay convenient for the code that builds them, and exactly
    one function decides what leaves the building.
    """
    # Absent means the caller's stage did not run KYC -- not that KYC ran and
    # found nothing. The two are different claims and the response must not
    # blur them: a FOS upload has no KYC verdict at all, while SKIPPED is a
    # verdict KYC reached.
    ran_kyc = envelope.get("kyc") is not None
    kyc = envelope.get("kyc") or {}
    conflicts = (
        response.conflicts_from_kyc(kyc)
        if los_config.conflict_detection_enabled()
        else []
    )
    documents = [
        response.compact_document(document)
        for document in envelope.get("documents") or []
    ]

    decision = response.decision_from(
        envelope.get("status") or "", kyc, conflicts,
        envelope.get("documents") or [],
    )
    processing = envelope.get("processing") or {}

    public: dict[str, Any] = {
        "request_id": envelope.get("request_id"),
        # Profile evidence, per party. Absent when no profile was
        # supplied for anyone -- an empty list would suggest matching
        # was attempted and found nothing.
        **({"profile_match": envelope["profile_match"]}
           if envelope.get("profile_match") else {}),
        "applicant_id": envelope.get("applicant_id"),
        # Present only when the case actually has a second party, so a
        # null on every single-applicant response cannot be read as
        # "there is a co-applicant and we do not know who".
        **({"co_applicant_id": envelope["co_applicant_id"]}
           if envelope.get("co_applicant_id") else {}),
        "case_id": envelope.get("case_id"),
        "status": envelope.get("status"),
        "documents": documents,
        # Cross-document agreement, as one object. The `conflicts` list it
        # replaces reported only disagreements, so "everything agreed" and
        # "nothing was comparable" both arrived as [].
        #
        # `conflicts` is still computed above -- decision and next_action are
        # derived from it -- it is simply no longer a separate public key
        # saying the same thing a second time.
        "cross_document": _public_cross_document(envelope, kyc),
        # WHETHER THE INCOME DOCUMENTS AGREE. Present only when this
        # stage ran the comparison, for the same reason `kyc` is: the
        # absence of a check and a check that found nothing comparable
        # are different answers.
        #
        # NOT PART OF `cross_document`, which is identity. A reviewer
        # reading a name mismatch and a salary difference in one list
        # would reasonably take them for the same kind of finding, and
        # only one of them is about who the applicant is.
        **({"income_consistency": _public_income_consistency(envelope)}
           if envelope.get("party_income") else {}),
        # AFFORDABILITY, AND NOT A LENDING DECISION. Present only when
        # this stage ran it. A PASS means the affordability policy is
        # satisfied on the evidence available; it does not mean the loan
        # is approved, and `decision` above is computed without reading
        # a single field of it.
        **({"eligibility": _public_eligibility(envelope)}
           if envelope.get("party_eligibility") else {}),
        # RISK SIGNALS, READ BY NOTHING ABOVE. The risk agent has always
        # existed and has never been reachable from this journey; it is
        # now, consuming the FOIR eligibility published rather than
        # computing a second one. `decision` and `next_action` are
        # unchanged and do not read it.
        **({"risk": _public_risk(envelope)}
           if envelope.get("party_risk") else {}),
        # A word, not an object. The reasons behind it are already carried by
        # kyc.reason_codes and the cross-document checks, and repeating them
        # here made the same codes appear three times in one response.
        "decision": decision["status"],
        "next_action": response.next_action_from(
            envelope.get("status") or "", kyc, conflicts, documents
        ),
        "summary": envelope.get("summary", ""),
        "summary_source": envelope.get("summary_source"),
        "processing_ms": round(float(processing.get("total_ms") or 0.0), 2),
        "errors": envelope.get("errors") or [],
    }

    sections = _party_sections(envelope, documents)
    public.update(sections)

    # THE APPLICATION, IN ONE OBJECT.
    #
    # Purely a regrouping of values already published above: the status
    # the roll-up produced, the decision the decision rules made, the
    # next action they chose, and each party's own recorded reasons --
    # now tagged with WHOSE they are. Nothing here ranks, overrides or
    # re-derives any of it, which is why it can be added to a response
    # that decides loans without changing a single outcome.
    public["overall"] = overview.build(
        status=str(envelope.get("status") or ""),
        decision=decision["status"],
        next_action=public["next_action"],
        sections=sections,
        documents=documents,
    )

    # KYC, ONLY WHERE A STAGE ACTUALLY RAN IT.
    #
    # /api/v1/los/process runs it and this key is always present there, so
    # the LOS contract is unchanged. A caller that switched cross-document
    # checks off -- the FOS stage does -- gets no key at all, because it has
    # no KYC verdict to report and an object reading SKIPPED would be one.
    if ran_kyc:
        # The same shaper the party sections use, so the case-level
        # object and a party's own can never describe one result
        # differently.
        # COMPACT ON A TWO-PARTY CASE. Every field row is already
        # published under the party it belongs to; a second full copy
        # here said the same thing again and could not say whose row
        # was whose. On a single-applicant case the top-level object is
        # that party's only KYC, so it keeps its rows and the existing
        # contract is untouched.
        public["kyc"] = response.public_kyc(
            kyc, compact=bool(envelope.get("co_applicant_id")))

    # NO STAGE-TIMING OBJECT.
    #
    # ocr_ms, classification_ms, specialist_ms, mcp_ms, orchestration_ms,
    # summary_ms and llm_ms describe how this service is built. They stay on
    # the internal envelope and in the orchestration logs, where the people
    # tuning it can read them, rather than becoming a contract a client
    # starts depending on. `processing_ms` -- how long the call took -- is
    # the one number a client has a use for, and it is above.

    return public


__all__ = [
    "process_application", "UploadedDocument", "document_mode",
    "PROCESS", "PUBLIC_OPERATIONS",
]
