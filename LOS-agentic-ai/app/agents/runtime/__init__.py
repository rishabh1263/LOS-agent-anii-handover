"""
The COMMON agent runtime (harness) -- shared infrastructure for every LOS agent.

    harness.py     run an agent: run id, ids, caller, deadline, limits, OTEL,
                   output validation, normalised errors, audit + eval hooks
    execution.py   RunContext: the per-run handle an agent uses to call tools
                   and models (bounded retries, timeouts, tracking, spans)
    limits.py      Limits (configured bounds) and Budget (what is left)
    trajectory.py  the recorded path: events, tool calls, model calls,
                   redaction, deterministic replay representation
    errors.py      the normalised error vocabulary
    evals/         agent-agnostic eval contracts and runner

DOMAIN-AGNOSTIC BY RULE. Nothing here knows about credit, underwriting,
eligibility, KYC, documents, risk or decisions. An agent brings its own
domain engine (typically a LangGraph) and its own output checks; the harness
brings the controls every agent needs. The Credit Underwriting Agent is the
first consumer; other agents migrate by wrapping their existing entry point
in `harness.run` (see docs in harness.py).
"""
