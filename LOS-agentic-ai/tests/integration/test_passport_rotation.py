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
    # 2026-10-15: since the MRZ finder accepts a short-name line and reads MRZ tokens on small pages, this photo is
    # read WITHOUT turning it -- from the upscaled pass, with every MRZ check digit verified (the rotate_90 read it
    # used to need gave the same name / DOB / number but did NOT verify). Asserted: the stronger outcome, a
    # check-digit-verified read; rotations stay the route for a page no other pass can read.
    assert r.fields["mrz_verified"].value is True
    assert any(w.startswith(("ocr_pass=rotate_", "ocr_pass=upscale")) for w in r.warnings)
