"""
THE AUDIT'S MIS-ROUTED PHRASINGS reach their intent by rule (2026-10-11, audit P2-K), and the new shapes take nothing
that is not theirs. The compound question is split, never rewritten into one half (fos_api._understood).
"""
import pytest

from app.agents.applicant.copilot.semantics import intents as i


@pytest.mark.parametrize("question, intent", [
    ("how far along is this application?", "APPLICATION_STAGE"),
    ("how far has this case progressed?", "APPLICATION_STAGE"),
    ("what exactly is blocking CPA?", "READINESS"),
    ("what's stopping this case from going to CPA?", "READINESS"),
    ("which documents did we already collect?", "DOCUMENTS_UPLOADED"),
    ("what have we collected so far?", "DOCUMENTS_UPLOADED"),
    ("what's the customer's date of birth on file?", "APPLICANT_PROFILE"),
])
def test_the_audit_phrasing_reaches_its_intent(question, intent):
    assert i.classify(question).intent.value == intent


@pytest.mark.parametrize("question, not_this", [
    ("how far is the branch from here", "APPLICATION_STAGE"),          # distance, not progress
    ("what documents are required for a personal loan?", "DOCUMENTS_UPLOADED"),
    ("is this case ready for CPA?", "APPLICATION_STAGE"),
])
def test_the_new_shapes_take_nothing_else(question, not_this):
    assert i.classify(question).intent.value != not_this


def test_the_compound_question_is_split():
    # both halves are answered: split in two, or -- both being RECORDED fields -- one profile answer carrying both
    # (2026-10-12: "who is the applicant" became the name field, so the multi-field profile path takes it)
    from app.agents.applicant.copilot.answering import profile

    q = "who is the applicant and what loan did they ask for?"
    parts = i.compound_parts(q)
    fields = profile.detect(q)
    assert parts == ["who is the applicant?", "what loan did they ask for?"] or \
        (fields is not None and {"full_name", "product"} <= set(fields.fields)), (parts, fields)


def test_the_catalogue_canonical_for_uploaded_documents_is_a_rule_question():
    from app.agents.applicant.copilot.semantics import meaning

    spec = meaning.catalogue()["intents"]["uploaded_documents"]
    assert i.classify(spec["canonical"]).intent.value == "DOCUMENTS_UPLOADED"


@pytest.mark.parametrize("message, closes", [
    ("close", True), ("back", True), ("close this case please", True), ("go back", True), ("bahar aao", True),
    ("case band karo", True), ("back to list", True),
    ("how close is this case to CPA?", False), ("back to the case: what is pending?", False),
    ("back to the case", False), ("what is the closing date", False),
])
def test_an_exit_phrase_closes_only_as_a_command(message, closes):
    from app.agents.applicant.copilot.capabilities import workspace

    assert workspace._says("exit", message) is closes
