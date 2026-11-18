"""
The Applicant Agent: a FOS copilot over the case store.

Answers a field officer's questions about an applicant, their application,
the documents collected, what is outstanding and whether the case may be
handed to CPA -- in natural language, from real stored records.

WHAT IT IS NOT. It does not decide credit, risk, KYC, RCU or the loan. Those
questions are recognised and routed to the capability that owns them; this
agent answers none of them and invents no result on their behalf.

Layering, and it is enforced rather than conventional:

    copilot/agent.py                 the flow: classify, authorise, plan, call, phrase
    copilot/semantics/intents.py     what was asked, and which tools answer it
    copilot/conversation/state.py    what the conversation is about, turn to turn
    copilot/routing/                 which capability, party and model path applies
    copilot/answering/answer.py      phrasing, deterministic first and model second
    copilot/answering/validate.py    the model may not state a fact it was not given
    copilot/facts/                   stored facts, interpreted for an answer
    permissions.py                   who may ask it, and about whom
    workflow.py                      the deterministic business answers
    audit.py                         who asked what, and what changed

Data is reached only through app/mcp/applicant.py. Nothing here imports the
repository; the only sqlite3 use is the conversation store's own optional
backend (copilot/conversation/state.py), which holds labels, never case data.
"""

# LAZY (2026-11-02): importing a small part of this package -- permissions, audit, config (the eligibility API, the
# document desk use them) -- no longer loads the whole copilot; these three load on first use, exactly as before.
_LAZY = {"AgentError", "answer_question", "confirm_action"}


def __getattr__(name: str):
    if name in _LAZY:
        from app.agents.applicant.copilot import agent

        return getattr(agent, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")



__all__ = ["AgentError", "answer_question", "confirm_action"]
