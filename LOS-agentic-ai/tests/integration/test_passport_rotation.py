"""A sideways passport photo is identified from its header but read only when turned (2026-10-06)."""

from pathlib import Path

import pytest

SAMPLE = Path("samples/passports/passport_samples0_4.jpg")


@pytest.mark.skipif(not SAMPLE.exists(), reason="real sample not present")
def test_an_identified_document_with_no_required_field_read_gets_the_rotation_pass():
    from app.agents.document_agent import extract_document

    r = extract_document(str(SAMPLE))
    assert r.document_type.value == "PASSPORT"
    assert r.fields["date_of_birth"].value and r.fields["name"].value
    assert any(w.startswith("ocr_pass=rotate_") for w in r.warnings)
