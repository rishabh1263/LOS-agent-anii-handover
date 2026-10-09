"""
PHASE 3 STEP 6e -- response style + the abbreviation rule (COPILOT_RESPONSE_STYLE, default off).

Snapshots (English + Hinglish) of the styler on the CHATBOT_SPEC example shapes, the glossary
(first mention expanded once per session, PENDING terms never expanded, "X kya hai?" -> full form
+ one line), and the end-to-end session. Emoji map / next steps: applicant_agent.yaml
chatbot.response_style; glossary: app/config/glossary.yaml.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.answering import style

# ---- snapshots: the spec's example shapes ----------------------------------------------------
SNAPSHOTS = [
    # (intent, answer in, styled answer out)
    ("APPLICATION_STATUS", "Your application LN1023 is in FOS stage. Waiting on 2 documents.",
     "📍 Your application LN1023 is in **FOS (Field Officer Sales)** stage. Waiting on 2 documents.\n\n"
     "👉 Ask \"what is pending\" to see the documents."),
    ("APPLICATION_STATUS", "Aapka application LN1023 FOS stage mein hai. 2 documents baaki hain.",
     "📍 Aapka application LN1023 **FOS (Field Officer Sales)** stage mein hai. 2 documents baaki hain.\n\n"
     "👉 Ask \"what is pending\" to see the documents."),
    ("DOCUMENTS_PENDING", "Still pending:\n- Bank Statement",
     "⏳ Still pending:\n- Bank Statement\n\n👉 Upload these to move the case to **CPA**."),
    ("KYC_RESULT", "KYC verification failed. Name: PAN shows \"Rahul Kumar Sharma\", DL shows \"Rahul Sharma\".",
     "⚠️ **KYC (Know Your Customer)** verification failed. Name: **PAN (Permanent Account Number)** shows "
     "\"Rahul Kumar Sharma\", **DL (Driving Licence)** shows \"Rahul Sharma\".\n\n"
     "👉 Upload the correct document to fix the mismatch."),
    ("OUT_OF_SCOPE", "I can only help with your loan application.",
     "🙏 I can only help with your loan application.\n\n👉 Check your **status** or **pending documents**?"),
    ("FOS_KNOWLEDGE", "CIBIL score ek 3-digit number hai (300-900). Aapka credit check Credit team karti hai.",
     "ℹ️ **CIBIL (TransUnion CIBIL (formerly Credit Information Bureau (India) Limited))** score ek 3-digit number hai "
     "(300-900). Aapka credit check Credit team karti hai.\n\n👉 Check your **status** or **pending documents**?"),
]


@pytest.mark.parametrize("intent,answer,expected", SNAPSHOTS, ids=[f"{s[0]}-{i}" for i, s in enumerate(SNAPSHOTS)])
def test_style_snapshots(intent, answer, expected):
    out, _ = style.apply({"intent": intent, "answer": answer}, explained=set())
    assert out["answer"] == expected


def test_an_answer_with_its_own_next_step_or_question_gets_no_second_one():
    assert style.format_answer("Done.\n\n👉 Upload the PAN.", "DOCUMENTS_PENDING").count("👉") == 1
    assert "👉" not in style.format_answer("Which one do you mean?", "APPLICATION_STATUS")


def test_an_answer_that_already_has_an_emoji_keeps_it():
    assert style.format_answer("✅ All documents are verified.", "APPLICATION_STATUS").startswith("✅ All")


# ---- the abbreviation rule ----------------------------------------------------------------------
def test_first_mention_expanded_second_not():
    first, newly = style.expand_first_mentions("KYC failed. KYC again.", set())
    assert first == "**KYC (Know Your Customer)** failed. KYC again." and newly == {"KYC"}
    second, again = style.expand_first_mentions("KYC is still failing.", newly)
    assert second == "KYC is still failing." and not again


@pytest.mark.parametrize("term", ["CPA", "BOPS", "HOPS", "RCU", "JEV"])
def test_pending_terms_never_show_a_full_form(term):
    text, newly = style.expand_first_mentions(f"The case moved to {term}.", set())
    assert text == f"The case moved to {term}." and not newly
    assert style.definition(term, "en").startswith(f"ℹ️ **{term}**:")


def test_ovd_and_aa_are_not_in_the_glossary():
    assert "OVD" not in style.terms() and "AA" not in style.terms()


def test_ids_are_never_expanded():
    text, _ = style.expand_first_mentions("CASE-PAN12 and APP-KYC9", set())
    assert text == "CASE-PAN12 and APP-KYC9"


@pytest.mark.parametrize("question,term", [("KYC kya hai?", "KYC"), ("KYC ka full form?", "KYC"),
                                           ("what is FOIR?", "FOIR"), ("EMI kya hota hai", "EMI"),
                                           ("CPA ka matlab", "CPA")])
def test_x_kya_hai_finds_the_term(question, term):
    assert style.defined_term(question) == term


def test_a_case_question_is_not_a_definition():
    assert style.defined_term("mera KYC kya hai?") is None


def test_kyc_kya_hai_gives_full_form_and_meaning_in_hinglish():
    assert style.definition("KYC", "hi-Latn") == "ℹ️ **KYC (Know Your Customer)**: aapki identity verify karne ki process."


# ---- end to end -------------------------------------------------------------------------------------
@pytest.fixture
def styled(monkeypatch):
    monkeypatch.setenv("COPILOT_RESPONSE_STYLE", "true")


def test_flag_off_changes_nothing(client):
    from tests.integration.test_fos_stage_boundary import open_case

    a, c = open_case(client)
    body = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c, "message": "KYC kya hai?"}).json()
    assert body.get("response_source") != "GLOSSARY" and "response_style" not in body


def test_glossary_answer_and_session_memory_end_to_end(client, styled):
    from tests.integration.test_fos_stage_boundary import open_case

    a, c = open_case(client)
    first = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c,
                                                     "message": "mera status kya hai?"}).json()
    assert first["answer"][:2].strip() in ("📍", "⏳", "✅", "⚠️"), first["answer"]
    context = first.get("context")
    definition = client.post("/api/v1/fos/copilot", json={"applicant_id": a, "case_id": c,
                                                          "message": "KYC kya hai?", "context": context}).json()
    assert definition["response_source"] == "GLOSSARY"
    assert definition["answer"].startswith("ℹ️ **KYC (Know Your Customer)**")


from tests.integration.test_reupload_supersedes import _store, client  # noqa: E402,F401
