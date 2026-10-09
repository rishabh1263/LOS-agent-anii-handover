"""
CONTEXTUAL FRAGMENTS (owner 2026-10-09; capabilities/vague.py contextual): "docs" / "status" / "upload" read with
the conversation -- the previous question when it is one of the fragment's readings, one clear reading, else ONE
question with options; no case and no context -> what to do is asked. Document facts always come from the case store
(the engine's tools), never from the conversation. Real chat path; FAKE embedder (no model in the suite).
"""

from __future__ import annotations

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_meaning import FakeEmbedder, fake  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

ASKED = "What would you like to know about"


def say(c, message, chat):
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()
    r = c.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en",
                                            "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


def test_a_new_conversation_asks_what_to_do(client, prod, fake):
    make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    md = say(client, "docs", "frag-new")
    assert ASKED in md and md.count("](ask:") >= 2


def test_docs_after_pending_documents_answers_the_pending_list(client, prod, fake):
    _, c1 = make_case(client, "Rahul Sharma")
    say(client, f"open {c1}", "frag-pend")
    first = say(client, "what is pending", "frag-pend")
    md = say(client, "docs", "frag-pend")
    assert ASKED not in md
    assert "pending" in md.lower() and "PAN" in md                 # the case's own pending documents, read again
    assert first.split("\n")[0] == md.split("\n")[0]                # the same answer the question gave


def test_docs_after_a_verification_question_answers_that_documents_status(client, prod, fake):
    _, c1 = make_case(client, "Rahul Sharma")
    say(client, f"open {c1}", "frag-ver")
    say(client, "has the bank statement been verified", "frag-ver")
    md = say(client, "docs", "frag-ver")
    assert ASKED not in md and "Bank Statement" in md


def test_docs_after_an_uploaded_documents_question_stays_on_that_question(client, prod, fake):
    _, c1 = make_case(client, "Rahul Sharma")
    say(client, f"open {c1}", "frag-up")
    say(client, "is the PAN uploaded", "frag-up")
    md = say(client, "docs", "frag-up")
    assert ASKED not in md and "PAN" in md


def test_docs_after_switching_applicant_is_about_the_new_applicant_only(client, prod, fake):
    _, c1 = make_case(client, "Rahul Sharma")
    _, c2 = make_case(client, "Priya Verma")
    say(client, f"open {c1}", "frag-sw")
    say(client, "what is pending", "frag-sw")
    say(client, f"open {c2}", "frag-sw")
    md = say(client, "docs", "frag-sw")
    assert ASKED not in md
    assert c2 in md and c1 not in md                               # read for the applicant open NOW


# the REAL model's ranking of the fragment "upload" (nomic-embed-text, recorded 2026-10-09): the bag-of-words fake
# cannot rank a one-word fragment against multi-word examples, so this test pins the ranking (a test double)
REAL_UPLOAD_RANKING = [("process_upload", 0.846), ("pending_documents", 0.785), ("upload_document", 0.744),
                       ("command", 0.723)]


def test_upload_with_a_document_in_context_gives_that_upload_link(client, prod, fake, monkeypatch):
    from app.agents.applicant.copilot.semantics import meaning

    real_rank = meaning.rank
    monkeypatch.setattr(meaning, "rank", lambda m, e=None: REAL_UPLOAD_RANKING if m.strip() == "upload" else real_rank(m, e))
    _, c1 = make_case(client, "Rahul Sharma")
    say(client, f"open {c1}", "frag-upl")
    say(client, "has the bank statement been verified", "frag-upl")
    md = say(client, "upload", "frag-upl")
    assert "(action:upload?doc=BANK_STATEMENT" in md


def test_a_fragment_with_no_supporting_context_asks_once(client, prod, fake):
    _, c1 = make_case(client, "Rahul Sharma")
    say(client, f"open {c1}", "frag-amb")
    say(client, "what is the loan amount", "frag-amb")             # a previous question that is NOT a docs reading
    md = say(client, "docs", "frag-amb")
    assert ASKED in md and 2 <= md.count("](ask:") <= 4
