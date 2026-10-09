"""
FOS CONVERSATION FOUNDATION (directive 2026-10-09, phases 3-8): regression tests for every defect the real-model
scenario run (runs/fos_scenarios/) found. The chat path is the real one (/api/v1/fos/copilot, the test store); the
embedder is the FAKE bag-of-words one from test_meaning (no model in the suite).
"""

from __future__ import annotations

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_meaning import FakeEmbedder, fake  # noqa: F401
from tests.integration.test_reupload_supersedes import FOS_SCOPES, _store, client  # noqa: F401


def say(c, message, chat="found", **headers):
    from app.agents.applicant.copilot.capabilities import safety

    safety.reset()
    r = c.post("/api/v1/fos/copilot", headers=headers or None,
               json={"action": "CUSTOM_QUERY", "message": message, "reply_language": "en", "chat_id": chat})
    assert r.status_code == 200, r.text
    return r.json()["markdown"]


def amount(case_id):
    from app.store import get_repository

    return int(float(getattr(get_repository().get_application(case_id), "loan_amount", 0) or 0))


# ---- applicant switching ------------------------------------------------------------------------------------------
def test_an_officer_asking_for_another_applicant_is_switched_not_refused(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    say(client, f"open {c1}")
    md = say(client, "Ek minute, doosre applicant ka status dekhna hai.")
    assert "other customers" not in md and "can't provide" not in md
    assert "Priya" in md                                   # the officer's OWN cases to pick from


def test_back_to_the_previous_applicant(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    _, c2 = make_case(client, "Priya Verma")
    say(client, f"open {c1}")
    say(client, f"open {c2}")
    md = say(client, "Achha, ab pehle wale par wapas chalo.")
    assert c1 in md and "can't find" not in md


def test_a_named_applicant_question_is_answered_for_that_applicant_only(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    _, c2 = make_case(client, "Priya Verma")
    say(client, f"open {c1}")
    md = say(client, "Priya ka pending kya hai?")
    assert c2 in md and c1 not in md                       # never the open case's answer for another person
    assert "other customers" not in md


def test_a_bare_name_selects_the_case_never_a_vague_question(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    _, c2 = make_case(client, "Priya Verma")
    say(client, f"open {c1}")
    md = say(client, "Priya Verma")
    assert c2 in md and "What would you like to know" not in md


# ---- session isolation and recovery -------------------------------------------------------------------------------
def test_the_conversation_survives_a_restart_and_never_reaches_another_officer(client, prod, make_token):
    _, c1 = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")
    say(client, f"open {c1}", chat="recover")
    from app.agents.applicant.copilot.conversation import state as conv

    conv.STORE._memory = conv.ConversationStore()          # the process's caches gone; the store keeps the chat
    conv.STORE._repository = None
    assert c1 in say(client, "kya pending hai?", chat="recover")
    other = {"Authorization": f"Bearer {make_token(subject='officer-b', scopes=FOS_SCOPES)}"}
    md = say(client, "kya pending hai?", chat="recover", **other)
    assert c1 not in md and "Rahul" not in md


# ---- hold / ambiguous action (meaning, fake embedder) ------------------------------------------------------------
def test_hold_drops_a_pending_draft_and_nothing_is_written(client, prod, fake):
    _, c1 = make_case(client, "Rahul Sharma", loan_amount=500000)
    say(client, f"open {c1}")
    assert "700000" in say(client, "loan amount 7 lakh karo")
    md = say(client, "ruko abhi action mat lena")
    assert "nothing was written" in md.lower()
    assert amount(c1) == 500000
    assert "Updated" not in say(client, "confirm")         # the dropped draft can never be confirmed afterwards
    assert amount(c1) == 500000


def test_an_ambiguous_action_asks_which_supported_action(client, prod, fake):
    _, c1 = make_case(client, "Rahul Sharma")
    say(client, f"open {c1}")
    md = say(client, "usko process kar do")
    assert "What should I do" in md and md.count("](ask:") >= 3
    assert "Updated" not in md and "moved" not in md.lower()


# ---- links, follow-ups, answers ----------------------------------------------------------------------------------
def test_link_targets_keep_their_document_code():
    from app.agents.applicant.copilot.answering import contract

    out = contract._labels("[Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) DRIVING_LICENCE")
    assert "doc=DRIVING_LICENCE" in out and out.endswith("Driving Licence")


def test_a_correction_is_the_previous_question_for_the_new_document():
    from app.agents.applicant.copilot.semantics import meaning

    d = meaning._followup("Nahi, mera matlab bank statement se tha.", {"intent": "case_status"})
    assert d is not None and d.intent == "document_status" and "Bank Statement" in d.canonical
    assert meaning._followup("upload new bank statement", {"intent": "pending_documents"}) is None


def test_a_model_answer_that_echoes_the_question_is_never_shown():
    from app.agents.applicant.copilot.capabilities import general

    spec = general.cfg().get("general_llm") or {}
    assert general._poor_general_answer("Policy mein kya requirement hai, depend ke liye lender.",
                                        "Policy mein exactly kya requirement hai?", spec)
    assert not general._poor_general_answer(
        "A balance transfer moves an existing loan to another lender, usually for a lower interest rate.",
        "what is balance transfer", spec)


@pytest.mark.parametrize("word", ["yes", "no", "Yes", "kyu", "why", "help"])
def test_replies_and_bare_follow_ups_are_never_vague(word):
    from app.agents.applicant.copilot.capabilities import vague

    assert not vague.is_vague(word)


def test_the_next_step_carries_its_upload_link(client, prod):
    _, c1 = make_case(client, "Rahul Sharma")
    say(client, f"open {c1}")
    md = say(client, "what should I do next?")
    assert "Next step" in md and "(action:upload?doc=" in md


# ---- upload actions (owner 2026-10-09: "after opening a case it should allow upload; pending -> upload action") ----
def test_an_opened_case_offers_an_upload_button_for_every_pending_document(client, prod):
    import re

    _, c1 = make_case(client, "Rahul Sharma")
    md = say(client, f"open {c1}", chat="up-open")
    docs = re.findall(r"\(action:upload\?doc=([A-Z_]+)", md)
    assert {"PAN", "ADDRESS_PROOF", "BANK_STATEMENT", "SIGNATURE"} <= set(docs), md


def test_the_upload_link_and_the_upload_itself_work_and_the_reply_offers_whats_left(client, prod):
    import re
    from pathlib import Path

    _, c1 = make_case(client, "Rahul Sharma")
    md = say(client, f"open {c1}", chat="up-flow")
    href = re.search(r"\((action:upload\?doc=PAN[^)]*)\)", md).group(1)
    tapped = client.post("/api/v1/fos/action", json={"href": href, "chat_id": "up-flow"}).json()
    assert tapped["type"] == "upload" and tapped["document_type"] == "PAN"
    image = (Path(__file__).resolve().parents[2] / "samples" / "documents" / "pandemo.png").read_bytes()
    r = client.post(tapped["post_to"], data={"action": "UPLOAD_DOCUMENT", "chat_id": "up-flow", "document_types": "PAN"},
                    files={"files": ("pandemo.png", image, "image/png")})
    assert r.status_code == 200
    after = r.json()["markdown"]
    assert "PAN" in after and "(action:upload?doc=SIGNATURE" in after     # what is still pending, as buttons


def test_no_upload_button_for_an_unidentified_document(client, prod):
    import io

    _, c1 = make_case(client, "Rahul Sharma")
    say(client, f"open {c1}", chat="up-unk")
    client.post("/api/v1/fos/copilot?chat_id=up-unk", data={"action": "UPLOAD_DOCUMENT", "chat_id": "up-unk"},
                files={"files": ("x.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "image/png")})
    md = say(client, "what is pending", chat="up-unk")
    assert "doc=UNKNOWN" not in md and "(action:upload?doc=PAN" in md
