"""
The Applicant Agent: a FOS copilot over the case store.

Answers a field officer's questions about an applicant, their application,
the documents collected, what is outstanding and whether the case may be
handed to CPA -- in natural language, from real stored records.

WHAT IT IS NOT. It does not decide credit, risk, KYC, RCU or the loan. Those
questions are recognised and routed to the capability that owns them; this
agent answers none of them and invents no result on their behalf.

Layering, and it is enforced rather than conventional:

    agent.py        the flow: classify, authorise, plan, call, phrase
    intents.py      what was asked, and which tools answer it
    permissions.py  who may ask it, and about whom
    workflow.py     the deterministic business answers
    answer.py       phrasing, deterministic first and model second
    validate.py     the model may not state a fact it was not given
    audit.py        who asked what, and what changed

Data is reached only through app/mcp/applicant.py. Nothing here imports the
repository, and nothing here imports sqlite3.
"""

from app.agents.applicant.agent import AgentError, answer_question, confirm_action

__all__ = ["AgentError", "answer_question", "confirm_action"]
