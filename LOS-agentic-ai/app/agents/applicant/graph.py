"""
The FOS copilot as a LangGraph.

    START -> context -> route -+-> case       -+-> respond -> END
                               |-> knowledge  -|
                               |-> mixed      -|
                               +-> downstream -+

WHAT THE GRAPH IS FOR, and it is not decoration. Four question categories
need four different sets of work, and the expensive mistake is doing work a
category does not need: retrieval for a checklist lookup, a record read for a
policy question, a model call for either. The router picks one branch and the
others never run.

MIXED IS THE ONE THAT PAYS FOR ITSELF. Its two halves -- structured case data
and knowledge retrieval -- are independent, so they run CONCURRENTLY. Done in
sequence a mixed question costs the sum; here it costs the slower of the two.

WHAT DOES NOT LIVE HERE. No SQL, no filesystem, no vector store. The case
branch reaches the store only through MCP tools, and the knowledge branch
only through the retriever. The graph decides WHICH capability runs; it never
becomes one.

LangGraph is optional at runtime. `APPLICANT_AGENT_GRAPH=false` falls back to
the same functions called in sequence, so a deployment without the dependency
loses the concurrency and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, TypedDict

from app.agents.applicant.intents import Classification, Intent
from app.agents.applicant.routing import QueryCategory, ResponseSource

logger = logging.getLogger(__name__)


def graph_enabled() -> bool:
    return (os.getenv("APPLICANT_AGENT_GRAPH", "true").strip().lower()
            not in {"false", "0", "no"})


class CopilotState(TypedDict, total=False):
    """What flows between nodes. Plain data; no live handles."""

    message: str
    applicant_id: str | None
    case_id: str | None
    classification: Classification
    category: str

    # Filled by the branches.
    results: dict[str, Any]
    tool_trace: list[dict[str, Any]]
    errors: list[dict[str, str]]
    knowledge: dict[str, Any] | None
    knowledge_answer: str | None
    knowledge_source: str | None
    route_to: str | None
    answer: str
    response_source: str
    timings: dict[str, float]


# ==========================================================================
# NODES
# ==========================================================================

async def route(state: CopilotState) -> CopilotState:
    """Decide the branch. Deterministic; the model is not consulted."""
    from app.agents.applicant.routing import category_for

    classification = state["classification"]
    state["category"] = category_for(classification.intent).value
    return state


async def case_node(state: CopilotState) -> CopilotState:
    """Structured case data, through MCP tools only."""
    from app.agents.applicant.agent import _call_tools
    from app.agents.applicant.intents import plan_for

    started = time.perf_counter()
    classification = state["classification"]

    plan = plan_for(classification, has_case=bool(state.get("case_id")))
    results, trace, errors = await _call_tools(
        plan,
        applicant_id=state.get("applicant_id"),
        case_id=state.get("case_id"),
        document_type=classification.document_type,
    )

    state["results"] = results
    state["tool_trace"] = trace
    state["errors"] = errors
    state.setdefault("timings", {})["case_ms"] = round(
        (time.perf_counter() - started) * 1000, 2)
    return state


async def knowledge_node(state: CopilotState) -> CopilotState:
    """FOS knowledge retrieval. Reads no records."""
    from app.agents.applicant import knowledge_answer as ka

    started = time.perf_counter()
    text, source, detail = await ka.answer(state["message"])

    state["knowledge_answer"] = text
    state["knowledge_source"] = source
    state["knowledge"] = detail
    state.setdefault("timings", {})["knowledge_ms"] = round(
        (time.perf_counter() - started) * 1000, 2)
    return state


async def mixed_node(state: CopilotState) -> CopilotState:
    """
    Both halves, CONCURRENTLY.

    They share no state and neither can influence the other, so running them
    in sequence would only add the smaller of the two to every mixed answer
    for nothing.
    """
    started = time.perf_counter()
    await asyncio.gather(case_node(state), knowledge_node(state))
    state.setdefault("timings", {})["mixed_ms"] = round(
        (time.perf_counter() - started) * 1000, 2)
    return state


async def downstream_node(state: CopilotState) -> CopilotState:
    """
    Routed. Nothing is read and nothing is retrieved.

    A credit question must not touch the store or the handbook on its way to
    being declined -- reading first and refusing afterwards is how a refusal
    starts leaking what it found.
    """
    from app.agents.applicant import config

    classification = state["classification"]
    table = config.routing_table().get(classification.route_to or "", {})

    state["route_to"] = table.get("route_to", classification.route_to)
    state["answer"] = table.get(
        "message", "That question is handled by a downstream process.")
    state["response_source"] = ResponseSource.ROUTED.value
    return state


def branch_for(state: CopilotState) -> str:
    """The conditional edge."""
    return {
        QueryCategory.CASE_ONLY.value: "case",
        QueryCategory.KNOWLEDGE_ONLY.value: "knowledge",
        QueryCategory.MIXED.value: "mixed",
        QueryCategory.DOWNSTREAM.value: "downstream",
    }.get(state.get("category", ""), "case")


# ==========================================================================
# ASSEMBLY
# ==========================================================================

_GRAPH = None


def build():
    """
    Compile the graph once.

    Returns None when LangGraph is unavailable or switched off, and the
    caller runs the same nodes in sequence instead.
    """
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH
    if not graph_enabled():
        return None

    try:
        from langgraph.graph import END, START, StateGraph
    except Exception:
        logger.info("LangGraph unavailable; FOS copilot runs its nodes "
                    "directly.")
        return None

    graph = StateGraph(CopilotState)
    graph.add_node("route", route)
    graph.add_node("case", case_node)
    graph.add_node("knowledge", knowledge_node)
    graph.add_node("mixed", mixed_node)
    graph.add_node("downstream", downstream_node)

    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", branch_for, {
        "case": "case",
        "knowledge": "knowledge",
        "mixed": "mixed",
        "downstream": "downstream",
    })
    for node in ("case", "knowledge", "mixed", "downstream"):
        graph.add_edge(node, END)

    _GRAPH = graph.compile()
    return _GRAPH


def reset() -> None:
    """Drop the compiled graph. For tests and configuration reloads."""
    global _GRAPH
    _GRAPH = None


async def run(state: CopilotState) -> CopilotState:
    """
    Execute the graph, or the equivalent sequence without it.

    The fallback is deliberately the same functions in the same order, so a
    deployment without LangGraph behaves identically and only loses the
    concurrency inside `mixed`.
    """
    compiled = build()

    if compiled is None:
        await route(state)
        node = {"case": case_node, "knowledge": knowledge_node,
                "mixed": mixed_node, "downstream": downstream_node}[
            branch_for(state)]
        return await node(state)

    result = await compiled.ainvoke(state)
    return dict(result)


__all__ = [
    "CopilotState", "branch_for", "build", "case_node", "downstream_node",
    "graph_enabled", "knowledge_node", "mixed_node", "reset", "route", "run",
]
