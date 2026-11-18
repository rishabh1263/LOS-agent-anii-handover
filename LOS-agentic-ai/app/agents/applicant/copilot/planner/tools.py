"""
THE PLANNER'S TOOLS -- thin, typed wrappers over the EXISTING deterministic handlers (no business logic here):

    case_question       one fact about the open case          -> the engine (answer_question) with the catalogue's
                                                                 canonical question for that topic
    list_cases          the officer's own cases                -> case_list.run + case_list.render
    portfolio_analytics count / total / average / group / compare over own cases -> analytics.run (validated Query)
    handbook            the approved FOS handbook              -> general._knowledge_candidate (BM25 + relevance guard)
    next_stage          the configured workflow after a stage  -> conversation_general.yaml stage_sequence

Every value a reply states comes from these handlers; the model only chooses the tool and its arguments.
"""

from __future__ import annotations

from typing import Any, Literal

from app.agents.applicant.copilot.planner.registry import ToolContext, tool

CaseTopic = Literal["case_summary", "case_status", "pending_documents", "uploaded_documents", "kyc_status",
                    "cpa_readiness", "next_step", "why_stuck", "application_details", "document_rejection"]
CaseFilter = Literal["all", "needs_action", "kyc_issue", "documents_pending", "ready_for_cpa", "stuck"]
CaseSort = Literal["needs_action", "recent", "oldest", "longest_in_stage"]
Stage = Literal["FOS", "CPA", "CREDIT", "RCU", "BOPS", "HOPS", "DISBURSEMENT"]
Period = Literal["today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_year",
                 "last_year"]


def _canonical(topic: str) -> str | None:
    from app.agents.applicant.copilot.semantics import meaning

    spec = (meaning.catalogue().get("intents") or {}).get(topic) or {}
    return str(spec.get("canonical") or "") or None


#: what each topic answers (put into the tool's description: the model chooses from these words)
CASE_TOPICS = {
    "case_summary": "an overview of the case", "case_status": "its status / stage / whether it is approved or decided",
    "pending_documents": "documents still pending or missing", "uploaded_documents": "documents already uploaded",
    "kyc_status": "the KYC result", "cpa_readiness": "whether it can move / go to CPA now",
    "next_step": "what the officer should do next", "why_stuck": "why it is stuck / not moving",
    "application_details": "an applicant or loan detail (name, loan amount, tenure, product, income)",
    "document_rejection": "why a document was rejected",
}


@tool("case_question", "Answer ONE question about the case that is open now. topic: "
      + "; ".join(f"{k} = {v}" for k, v in CASE_TOPICS.items()) + ". Needs an open case.", needs_case=True)
async def case_question(ctx: ToolContext, topic: CaseTopic, detail: str | None = None) -> dict[str, Any] | None:
    from app.agents.applicant.copilot.agent import answer_question

    canonical = detail if (topic == "application_details" and detail) else _canonical(topic)
    if not canonical:
        return None
    return await answer_question(message=canonical, applicant_id=ctx.applicant_id, case_id=ctx.case_id,
                                 claims=ctx.claims, request_id=ctx.request_id, intent_override=None, concise=True,
                                 context=None)


@tool("list_cases", "List the officer's OWN cases, optionally only those with a KYC issue / documents pending / ready "
      "for CPA / stuck, sorted (oldest first, most recent, needing action first, longest in stage).")
async def list_cases(ctx: ToolContext, filter: CaseFilter = "all", sort: CaseSort = "needs_action",
                     limit: int = 5) -> dict[str, Any] | None:
    from app.agents.applicant.copilot.capabilities import case_list, general

    limit = max(1, min(int(limit), int(case_list.cfg().get("max_page_size", 20))))
    page = case_list.run(ctx.claims, case_list.ListQuery(filter=None if filter == "all" else filter, sort=sort,
                                                         size=limit))
    reply = general._reply(ctx.request_id, "CASE_LIST", case_list.render(page, ctx.lang), category="CASE_ONLY")
    reply.update(query_type="CASE_LIST", response_source="STRUCTURED", tools_invoked=["case.list"],
                 listed_case_ids=[r["case_id"] for r in page.rows])
    return reply


@tool("portfolio_analytics", "Count / total / average / lowest / highest over the officer's OWN cases, optionally "
      "grouped (by stage, status, product, month, what each case needs), in a period, or compared with another "
      "period. Metrics: cases, loan_amount, tenure_months, declared_monthly_income.")
async def portfolio_analytics(ctx: ToolContext, op: Literal["count", "sum", "avg", "min", "max"] = "count",
                              metric: Literal["cases", "loan_amount", "tenure_months",
                                              "declared_monthly_income"] = "cases",
                              group_by: Literal["stage", "status", "product", "month", "situation"] | None = None,
                              period: Period | None = None, compare_period: Period | None = None,
                              stage: Stage | None = None) -> dict[str, Any] | None:
    from datetime import date

    from app.agents.applicant.copilot.capabilities import analytics, general

    today = analytics._today()
    q = analytics.Query(op=op, metric=metric, group_by=group_by,
                        filters={"stage": stage} if stage else {},
                        period=analytics._named_period(period, today) if period else None,
                        compare=analytics._named_period(compare_period, today) if compare_period else None)
    if q.op != "count" and q.metric == "cases":
        q.metric = "loan_amount"
    analytics.validate(q)
    result = analytics.run(ctx.claims, q)
    reply = general._reply(ctx.request_id, "CASE_ANALYTICS", analytics.render(result), category="CASE_ONLY")
    reply.update(query_type="CASE_ANALYTICS", response_source="STRUCTURED", tools_invoked=["case.analytics"],
                 analytics={"query": q.as_dict()})
    _ = date
    return reply


@tool("handbook", "Look up the approved FOS / LOS handbook (process, stages, documents, policies, terms like FOIR, "
      "CIBIL, CPA). Use for any question about how the LOS works.")
async def handbook(ctx: ToolContext, query: str) -> dict[str, Any] | None:
    from app.agents.applicant.copilot.capabilities import general

    found = await general._knowledge_candidate(query[:300], ctx.request_id, None, False)
    if found is not None:
        return found[1]
    defined = await general._definition(f"what is {query}" if len(query.split()) <= 3 else query, ctx.request_id,
                                        ctx.lang, None)
    return defined


@tool("next_stage", "The stages that come after a given LOS stage, from the configured workflow.")
async def next_stage(ctx: ToolContext, stage: Stage) -> dict[str, Any] | None:
    from app.agents.applicant.copilot.capabilities import general

    said = general._after_stage(f"what comes after {stage.lower()}", ctx.lang)
    if not said:
        return None
    reply = general._reply(ctx.request_id, "STAGE_PROCESS", said, query_type="PROCESS_KNOWLEDGE")
    reply.update(response_source="STRUCTURED", tools_invoked=["workflow.stage_sequence"])
    return reply


__all__ = ["case_question", "handbook", "list_cases", "next_stage", "portfolio_analytics"]
