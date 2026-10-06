"""The structured response contract is derived from the envelope, never authored."""

from app.agents.applicant.copilot.answering import presentation


def test_list_answer_becomes_sections_with_icon_items():
    env = {"answer": "Documents on this application:\n✓ PAN — Verified\n✗ Bank Statement — Rejected — upload a correct one"
                     "\n\nNext step: Bank Statement needs attention.",
           "documents": [{"document_type": "PAN", "status": "VERIFIED"},
                         {"document_type": "AADHAAR", "status": "REVIEW", "party_role": "CO_APPLICANT"}],
           "next_action": {"label": "Upload the bank statement", "action": "UPLOAD"},
           "available_actions": [{"label": "Upload the bank statement"}, {"label": "Check KYC", "action": "KYC"}],
           # the bank statement IS missing, so an upload action is supported by the state
           "pending_items": [{"code": "DOCUMENT_MISSING", "slot": "BANK_STATEMENT"}],
           "knowledge": {"sources": [{"title": "KYC guide", "source": "kyc.md"}]}}
    out = presentation.build(env)
    assert out["next_action"] == {"type": "UPLOAD_DOCUMENT", "document_type": "BANK_STATEMENT",
                                  "label": "Upload Bank Statement"}
    # with nothing missing or rejected, the generic upload action is NOT offered
    nothing_missing = presentation.build({**env, "pending_items": []})
    assert [a["label"] for a in nothing_missing["actions"]] == ["Check KYC"]
    assert out["message"] == "Next step: Bank Statement needs attention."
    [section] = out["sections"]
    assert section["title"] == "Documents on this application"
    assert section["items"][0] == {"icon": "✓", "label": "PAN", "status": "Verified"}
    assert section["items"][1]["label"] == "Bank Statement"
    assert out["documents"][1]["label"].startswith("Co-applicant's") and out["documents"][1]["icon"] == "⚠"
    assert [a["label"] for a in out["next_actions"]] == ["Upload the bank statement", "Check KYC"]
    assert out["citations"] == out["knowledge_sources"] == [
        {"title": "KYC guide", "source": "kyc.md", "type": "OKF", "version": None, "effective_date": None}]
    assert out["semantic_decisions"] == []


def test_an_action_code_is_never_shown_as_a_label():
    out = presentation.build({"answer": "x", "next_action": {"action": "SUBMIT_TO_CPA"}})
    assert out["next_action"]["label"] == "Submit to CPA" and out["next_step"] == "Submit to CPA"
    assert presentation._readable_action("RAISE_KYC_QUERY") == "Raise KYC query"


def test_plain_answer_has_message_and_no_sections():
    out = presentation.build({"answer": "Hello! How can I help with your loan application?"})
    assert out["message"].startswith("Hello") and out["sections"] == [] and out["documents"] == []


def test_the_document_list_renders_in_hindi_marathi_and_hinglish():
    from app.agents.applicant.copilot.answering import localize

    docs = [{"document_type": "PAN", "status": "VERIFIED"}, {"document_type": "BANK_STATEMENT", "status": "REJECTED"}]
    for lang, word in (("hi", "सत्यापित"), ("mr", "पडताळलेले"), ("hi-Latn", "Verified")):
        out = localize._documents_list_sentence({"documents": docs}, lang)
        lines = out.splitlines()
        assert lines[1] == f"✓ PAN — {word}" and lines[2].startswith("✗ Bank Statement — ")
        assert "Bank Statement" in lines[-1]
    assert localize._documents_list_sentence({"documents": [{"document_type": "PAN", "status": "ODD"}]}, "hi") is None


def test_document_cards_carry_state_reason_action_and_party_never_internals():
    out = presentation.build({"answer": "x", "request_id": "r1", "documents": [
        {"document_id": "C:A:pan.jpg", "document_type": "PAN", "status": "VERIFIED", "reason_codes": []},
        {"document_type": "BANK_STATEMENT", "status": "PROCESSING", "reason_codes": ["DOCUMENT_QUEUED_FOR_PROCESSING"]},
        {"document_type": "SALARY_SLIP", "status": "REJECTED", "reason_codes": ["DOCUMENT_TYPE_MISMATCH"]},
        {"document_type": "PAN", "status": "SUPERSEDED"},
        {"document_type": "AADHAAR", "status": "REVIEW", "party_role": "CO_APPLICANT"}]})
    docs = {d["document_type"]: d for d in out["documents"]}
    assert len(out["documents"]) == 4                                     # the superseded PAN is history
    assert docs["PAN"]["state"] == "VERIFIED" and docs["PAN"]["action_required"] is False
    assert docs["BANK_STATEMENT"]["state"] == "PROCESSING" and docs["BANK_STATEMENT"]["icon"] == "○"
    assert docs["SALARY_SLIP"]["action"] and docs["SALARY_SLIP"]["reason"]
    assert [g["party"] for g in out["document_groups"]] == ["Applicant", "Co-applicant"]
    assert out["status"] == "ACTION_REQUIRED" and out["metadata"]["request_id"] == "r1"
    text = str(out["documents"])
    assert "pan.jpg" not in text and "DOCUMENT_TYPE_MISMATCH" not in text and "document_id" not in text


def test_jev_decisions_are_advisory_items_and_never_acted_upon_unless_executed():
    out = presentation.build({"answer": "x", "semantic_decisions": {"semantic_decisions": [
        {"decision_type": "SEMANTIC_REVIEW", "status": "INFO", "confidence": 0.7, "target": "CASE"}]}})
    [item] = out["semantic_decisions"]
    assert item["type"] == "SEMANTIC_REVIEW" and item["subject"] == "CASE" and item["confidence"] == 0.7
    assert item["source"] == "JEV" and item["acted_upon"] is False
    assert {"provider", "model", "timestamp", "evidence_refs", "affected_documents", "decision"} <= set(item)
