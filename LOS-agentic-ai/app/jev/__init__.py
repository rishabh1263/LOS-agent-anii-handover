"""
JEV -- the LOS semantic DECISION layer.

    AUTHORITATIVE STATE (state.py) -> TYPED QUESTIONS (config/jev.yaml)
      -> ONE batched call to the real Jev API (client.py)
      -> typed decisions + probabilities + confidence (engine.py)
      -> deterministic action, gated (actions.py)
      -> persisted, append-only and idempotent (repository jev_runs)

JEV decides bounded questions; code executes; domain agents stay authoritative;
an LLM only explains. Independent of chat: events and the API call it.

Not to be confused with app/agents/applicant/jev.py, the older optional
annotation hook for the Copilot composer (off, no provider) -- notes, not
decisions.
"""

from app.jev.engine import evaluate, latest_decisions  # noqa: F401
