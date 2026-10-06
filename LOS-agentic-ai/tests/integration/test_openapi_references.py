"""Swagger has no dangling schema references; the chat request body is documented (2026-10-06)."""

import json
import re


def test_every_schema_reference_resolves_and_the_chat_request_is_documented():
    import main

    spec = main.app.openapi()
    schemas = spec["components"]["schemas"]
    refs = set(re.findall(r"#/components/schemas/([A-Za-z0-9_]+)", json.dumps(spec)))
    assert not (refs - set(schemas)), f"dangling: {sorted(refs - set(schemas))}"
    props = schemas["CopilotRequest"]["properties"]
    assert {"applicant_id", "case_id", "action", "message", "context", "response_language"} <= set(props)
