"""
The Credit Agent's tool allowlist -- and the only way it reads anything.

A CLOSED SET. The planner can only name a tool listed in TOOLS; anything else
is refused with ToolNotAllowed before it runs. Every tool is READ-ONLY.

  existing MCP capabilities, through the MCP runtime (same authorisation,
  same tracing, same protocol/in-process transport as the Copilot):
      applicant.get, application.get, documents.get, eligibility.get
  thin readers of recorded findings (adapters/):
      kyc.get, income.get, bank_behaviour.get, risk.get, financial_documents.get
  the bureau provider (bureau/):
      bureau.get                 -- per party; transient failure retried once

Each call returns a graded Observation plus a ToolCall record for the trace.
A tool never raises into the graph: a failure is an UNAVAILABLE observation.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.agents.credit.schemas import (
    EvidenceRef,
    EvidenceSource,
    Observation,
    Quality,
    ToolCall,
    UnderwritingContext,
)


from app.agents.runtime.errors import ToolNotAllowed  # noqa: E402  (one error, one vocabulary)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    category: str
    scope: str            # "case" | "party"
    kind: str             # "mcp" | "reader" | "bureau"


TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in (
    ToolSpec("applicant.get", "applicant", "party", "mcp"),
    ToolSpec("application.get", "application", "case", "mcp"),
    ToolSpec("documents.get", "documents", "case", "mcp"),
    ToolSpec("eligibility.get", "eligibility", "case", "mcp"),
    ToolSpec("kyc.get", "kyc", "case", "reader"),
    ToolSpec("income.get", "income", "case", "reader"),
    ToolSpec("bank_behaviour.get", "banking", "case", "reader"),
    ToolSpec("risk.get", "risk", "case", "reader"),
    ToolSpec("financial_documents.get", "income", "case", "reader"),
    ToolSpec("bureau.get", "bureau", "party", "bureau"),
)}


def spec(name: str) -> ToolSpec:
    found = TOOLS.get(name)
    if found is None:
        raise ToolNotAllowed(f"Tool {name!r} is not on the credit agent's allowlist.")
    return found


def _ref(source: EvidenceSource, record_type: str, record_id: str | None,
         field: str | None, value: Any = None, party_id: str | None = None,
         is_demo: bool = False) -> EvidenceRef:
    import hashlib

    digest = hashlib.sha1(
        f"{source.value}|{record_type}|{record_id}|{field}|{party_id}".encode()
    ).hexdigest()[:12]
    return EvidenceRef(ref_id=f"ev_{digest}", source=source, record_type=record_type,
                       record_id=record_id, field=field,
                       value_summary=None if value is None else str(value),
                       party_id=party_id, is_demo=is_demo)


# ---------------------------------------------------------------------------
# MCP tools -- graded from their envelopes
# ---------------------------------------------------------------------------

_DOC_KEYS = ("document_id", "document_type", "verification_status", "reason_codes", "party_id",
             "party_role")


def _grade_documents(result: dict[str, Any], ctx: UnderwritingContext) -> Observation:
    """
    The RECORDED verification verdict of each document -- never its presence.

    `verification_status` on a document record is copied by ingest from the
    document pipeline's own verdict (store/ingest.py; models.Document). That
    is the authoritative result this reads. A document with no recorded
    verdict is UNVERIFIED -- counted as such, never as passed. `status`
    (UPLOADED / VERIFIED / ...) is a derived workflow label and is not read.

    Quality: MISSING with no documents; LOW_CONFIDENCE when documents exist
    but none carries a recorded verdict; PRESENT otherwise.
    """
    documents = []
    for d in result.get("documents") or []:
        if not isinstance(d, dict):
            continue
        verdict = d.get("verification_status")
        row = {k: d.get(k) for k in _DOC_KEYS if k in d}
        row["verification_recorded"] = bool(verdict)
        documents.append(row)

    passed = [d for d in documents if str(d.get("verification_status") or "").upper() == "PASS"]
    not_passed = [d for d in documents if d["verification_recorded"]
                  and str(d["verification_status"]).upper() != "PASS"]
    unverified = [d for d in documents if not d["verification_recorded"]]

    if not documents:
        quality = Quality.MISSING
    elif not passed and not not_passed:
        quality = Quality.LOW_CONFIDENCE
    else:
        quality = Quality.PRESENT

    # The record is the DOCUMENT itself (its store id), not the case.
    evidence = [_ref(EvidenceSource.VERIFICATION, "DOCUMENT", d.get("document_id") or ctx.case_id,
                     f"{d.get('document_type')}.verification_status",
                     d.get("verification_status") or "NOT_RECORDED", d.get("party_id"))
                for d in documents]
    return Observation(
        tool="documents.get", category="documents", quality=quality,
        data={"documents": documents, "document_count": len(documents),
              "verified_pass_count": len(passed), "not_passed_count": len(not_passed),
              "unverified_count": len(unverified)},
        evidence=evidence)


def _grade_mcp(name: str, envelope: Any, ctx: UnderwritingContext,
               party_id: str | None) -> Observation:
    category = TOOLS[name].category
    status = getattr(envelope.status, "value", str(envelope.status))
    if not envelope.ok:
        quality = Quality.MISSING if status == "NOT_FOUND" else Quality.UNAVAILABLE
        code = envelope.error.code if envelope.error else status
        return Observation(tool=name, party_id=party_id, category=category,
                           quality=quality, error=code)

    result = envelope.result or {}
    if name == "application.get":
        application = result.get("application") or {}
        return Observation(
            tool=name, category=category, quality=Quality.PRESENT, data=application,
            evidence=[_ref(EvidenceSource.APPLICATION, "APPLICATION", ctx.case_id, field,
                           application.get(field))
                      for field in ("declared_monthly_income", "declared_monthly_obligations",
                                    "employment_type", "loan_amount")
                      if application.get(field) is not None])
    if name == "applicant.get":
        # Only that the party exists. Identifiers are not carried into the
        # assessment: underwriting does not need them.
        applicant = result.get("applicant") or {}
        return Observation(tool=name, party_id=party_id, category=category,
                           quality=Quality.PRESENT if applicant else Quality.MISSING,
                           data={"exists": bool(applicant)})
    if name == "documents.get":
        return _grade_documents(result, ctx)
    if name == "eligibility.get":
        if not result.get("recorded"):
            return Observation(tool=name, category=category, quality=Quality.MISSING,
                               data={"recorded": False})
        eligibility = result.get("eligibility") or {}
        return Observation(
            tool=name, category=category, quality=Quality.PRESENT, data=eligibility,
            evidence=[_ref(EvidenceSource.ELIGIBILITY, "FINDING:FINANCIAL/ELIGIBILITY",
                           ctx.case_id, "status", eligibility.get("status"))])
    return Observation(tool=name, party_id=party_id, category=category,
                       quality=Quality.PRESENT, data=result)


async def _run_mcp(name: str, ctx: UnderwritingContext, party_id: str | None,
                   caller: Any) -> tuple[Observation, str]:
    from app.mcp import runtime

    envelope, trace = await runtime.call(
        name, applicant_id=party_id or ctx.applicant_id, case_id=ctx.case_id,
        document_type=None, caller=caller, request_id=ctx.request_id,
        stage=ctx.stage, intent="CREDIT_UNDERWRITING")
    observation = _grade_mcp(name, envelope, ctx, party_id)
    return observation, str(trace.get("status") or ("OK" if envelope.ok else "ERROR"))


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

async def _run_reader(name: str, ctx: UnderwritingContext) -> tuple[Observation, str]:
    from app.agents.credit.adapters import bank, case_memory, financial_documents, risk

    readers: dict[str, Callable[[], Observation]] = {
        "kyc.get": lambda: case_memory.kyc_get(
            ctx.case_id, [p.party_id for p in ctx.parties], ctx.applicant_id),
        "income.get": lambda: case_memory.income_get(ctx.case_id),
        "bank_behaviour.get": lambda: bank.bank_behaviour_get(ctx.case_id, ctx.applicant_id),
        "risk.get": lambda: risk.risk_get(ctx.case_id),
        "financial_documents.get": lambda: financial_documents.financial_documents_get(
            ctx.case_id, ctx.applicant_id),
    }
    observation = await asyncio.to_thread(readers[name])
    return observation, "OK" if observation.quality is not Quality.MISSING else "NOT_FOUND"


# ---------------------------------------------------------------------------
# bureau
# ---------------------------------------------------------------------------

def _bureau_observation(report: Any, party_id: str, provider: Any) -> tuple[Observation, str]:
    if report is None:
        return (Observation(tool="bureau.get", party_id=party_id, category="bureau",
                            quality=Quality.MISSING, error="NO_BUREAU_RECORD",
                            is_demo=provider.is_demo), "NOT_FOUND")
    signals = report.signals()
    evidence = [_ref(EvidenceSource.BUREAU, f"BUREAU:{report.provider}", report.report_id,
                     field, signals.get(field), party_id, report.is_demo)
                for field in ("score", "history_months", "max_dpd_12m", "settlements",
                              "write_offs", "enquiries_6m", "total_monthly_obligation")
                if signals.get(field) is not None]
    data = {**signals, "provider": report.provider, "score_model": report.score_model,
            "report_id": report.report_id, "fetched_at": report.fetched_at}
    return (Observation(tool="bureau.get", party_id=party_id, category="bureau",
                        quality=Quality.PRESENT, data=data, evidence=evidence,
                        is_demo=report.is_demo), "OK")


# ---------------------------------------------------------------------------
# the one entry point -- every call runs through the COMMON harness
# ---------------------------------------------------------------------------

AGENT_ID = "credit_agent"


def detached_run(ctx: UnderwritingContext, caller: Any = None):
    """A single-call RunContext, for a tool executed outside an agent run."""
    from app.agents.credit import config
    from app.agents.runtime.execution import RunContext
    from app.agents.runtime.limits import Limits

    return RunContext.detached(
        AGENT_ID, request_id=ctx.request_id or "detached", caller=caller,
        allowlist=TOOLS.keys(),
        limits=Limits(max_tool_calls=1, max_retries=config.bureau_retries(),
                      deadline_seconds=config.timeout_seconds(),
                      call_timeout_seconds=max(config.bureau_timeout_seconds(), 5.0)))


async def execute(name: str, ctx: UnderwritingContext, *, party_id: str | None = None,
                  caller: Any = None, bureau_provider: Any = None, run: Any = None,
                  step: str | None = None) -> tuple[Observation, ToolCall]:
    """
    Run one allowlisted tool through the common harness (RunContext.call_tool):
    allowlist, budget, deadline, per-call timeout, the ONE bounded retry --
    only for a transient bureau failure -- tracking and the `agent.tool` span.

    Never raises except ToolNotAllowed: every failure is an UNAVAILABLE
    observation.
    """
    from app.agents.credit import bureau, config
    from app.agents.runtime.errors import DeadlineExceeded, LimitExceeded

    tool = spec(name)
    run = run or detached_run(ctx, caller)
    before = len(run.trajectory.tool_calls)
    provider = None
    kwargs: dict[str, Any] = {"party_id": party_id, "step": step, "max_retries": 0}

    if tool.kind == "mcp":
        attempt = lambda: _run_mcp(name, ctx, party_id, caller)  # noqa: E731
        kwargs["status_of"] = lambda r: r[1]
    elif tool.kind == "reader":
        attempt = lambda: _run_reader(name, ctx)  # noqa: E731
        kwargs["status_of"] = lambda r: r[1]
    else:
        party_id = party_id or ctx.applicant_id
        kwargs["party_id"] = party_id
        provider = bureau_provider or bureau.get_provider()
        timeout = config.bureau_timeout_seconds()
        attempt = lambda: provider.fetch(party_id, timeout=timeout)  # noqa: E731
        kwargs.update(timeout=timeout, transient=(bureau.BureauTimeout,),
                      max_retries=config.bureau_retries(),
                      status_of=lambda r: "OK" if r is not None else "NOT_FOUND")

    is_demo = bool(getattr(provider, "is_demo", False))
    try:
        result = await run.call_tool(name, attempt, **kwargs)
        if tool.kind == "bureau":
            observation, status = _bureau_observation(result, party_id, provider)
        else:
            observation, status = result
    except ToolNotAllowed:
        raise
    except (LimitExceeded, DeadlineExceeded) as exc:
        observation = Observation(tool=name, party_id=party_id, category=tool.category,
                                  quality=Quality.UNAVAILABLE, error=exc.code, is_demo=is_demo)
        status = "NOT_RUN"
    except (bureau.BureauTimeout, asyncio.TimeoutError, TimeoutError):
        observation = Observation(tool=name, party_id=party_id, category=tool.category,
                                  quality=Quality.UNAVAILABLE,
                                  error="BUREAU_TIMEOUT" if tool.kind == "bureau" else "TIMEOUT",
                                  is_demo=is_demo)
        status = "UNAVAILABLE"
    except bureau.BureauError:
        observation = Observation(tool=name, party_id=party_id, category=tool.category,
                                  quality=Quality.UNAVAILABLE, error="BUREAU_ERROR",
                                  is_demo=is_demo)
        status = "ERROR"
    except Exception as exc:  # a tool failure is an observation, not a crash
        observation = Observation(tool=name, party_id=party_id, category=tool.category,
                                  quality=Quality.UNAVAILABLE, error=type(exc).__name__,
                                  is_demo=is_demo)
        status = "ERROR"

    record = run.trajectory.tool_calls[-1] if len(run.trajectory.tool_calls) > before else None
    call = ToolCall(tool=name, party_id=party_id, status=status,
                    attempts=record.attempts if record else 0,
                    duration_ms=record.duration_ms if record else 0.0)
    return observation, call


__all__ = ["AGENT_ID", "TOOLS", "ToolNotAllowed", "ToolSpec", "detached_run", "execute", "spec"]
