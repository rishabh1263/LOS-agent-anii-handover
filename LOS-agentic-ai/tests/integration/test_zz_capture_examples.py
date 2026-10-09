"""
Capture the frontend contract examples (frontend_handoff/examples/*.json): REAL request/response pairs through the app
(in-process, test store), masked. Run explicitly:  CAPTURE_EXAMPLES=1 pytest tests/integration/test_zz_capture_examples.py
"""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("CAPTURE_EXAMPLES") != "1", reason="run explicitly to refresh the examples")
OUT = Path(__file__).resolve().parents[2] / "frontend_handoff" / "examples"


def _mask(value):
    text = json.dumps(value, ensure_ascii=False)
    text = re.sub(r"(CASE|APP|COAPP|QRY)-[0-9A-F]{6,}", lambda m: f"{m.group(1)}-XXXXXXXXXXXX", text)
    text = re.sub(r"(fos|cp|err)_[0-9a-f]{12,}", lambda m: f"{m.group(1)}_<request-id>", text)
    text = re.sub(r"\b\d{10}\b", "98XXXXXX10", text)
    return json.loads(text)


def _save(name: str, request: dict, response, *, status: int, headers=None, note: str = "") -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    body = response if isinstance(response, (dict, list, str)) else str(response)
    (OUT / f"{name}.json").write_text(json.dumps(_mask({
        "note": note, "request": request, "status": status,
        "headers": {k: v for k, v in (headers or {}).items() if k.lower() in (
            "content-type", "content-disposition", "x-contract-version")},
        "response": body}), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def test_capture(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    make_case(client, "Priya Verma")

    def chat(name, body, note=""):
        r = client.post("/api/v1/fos/copilot", json=body)
        _save(name, {"POST": "/api/v1/fos/copilot", "json": body}, r.json(), status=r.status_code,
              headers=r.headers, note=note)
        return r.json()

    chat("01_chat", {"action": "CUSTOM_QUERY", "message": "show my cases", "chat_id": "ex-1", "reply_language": "en"},
         "a typed message; the reply is markdown (a table with Open links) + tts")
    chat("02_case_open", {"action": "CUSTOM_QUERY", "message": f"open {case_id}", "chat_id": "ex-1",
                          "reply_language": "en"}, "the case brief: next step, one Upload link, Close")
    chat("03_follow_up_same_chat", {"action": "CUSTOM_QUERY", "message": "what is pending", "chat_id": "ex-1",
                                    "reply_language": "en"}, "same chat_id: answered for the open case")
    chat("04_vague", {"action": "CUSTOM_QUERY", "message": "docs", "chat_id": "ex-1", "reply_language": "en"},
         "one-word message: one question with options")
    chat("05_vague_pick", {"action": "CUSTOM_QUERY", "message": "1", "chat_id": "ex-1", "reply_language": "en"},
         "the pick answers at once")
    href = f"action:show_in_ui?case={case_id}"
    r = client.post("/api/v1/fos/action", json={"href": href, "chat_id": "ex-1"})
    _save("06_link_click_open_ui", {"POST": "/api/v1/fos/action", "json": {"href": href, "chat_id": "ex-1"}}, r.json(),
          status=r.status_code, headers=r.headers, note="an action: link -> open_ui route")
    href = f"action:download?format=xlsx&case={case_id}"
    r = client.post("/api/v1/fos/action", json={"href": href, "chat_id": "ex-1"})
    _save("07_download", {"POST": "/api/v1/fos/action", "json": {"href": href, "chat_id": "ex-1"}},
          f"<{len(r.content)} bytes of xlsx>", status=r.status_code, headers=r.headers,
          note="a file: save it with the Content-Disposition filename")
    files = {"files": ("pan.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "image/png")}
    form = {"action": "UPLOAD_DOCUMENT", "chat_id": "ex-1", "document_types": "PAN"}
    r = client.post("/api/v1/fos/copilot", data=form, files=files)
    _save("08_upload", {"POST": "/api/v1/fos/copilot (multipart)", "form": form, "files": ["pan.png"]},
          r.json() if "json" in r.headers.get("content-type", "") else r.text, status=r.status_code, headers=r.headers,
          note="multipart upload into the open case of this chat")
    body = {"action": "CUSTOM_QUERY", "message": "what is KYC", "chat_id": "ex-2", "reply_language": "en"}
    r = client.post("/api/v1/fos/copilot/stream", json=body)
    _save("09_stream", {"POST": "/api/v1/fos/copilot/stream", "json": body}, r.text, status=r.status_code,
          headers=r.headers, note="SSE: typing -> delta* -> final")
    body = {"action": "custom_query", "message": "hi", "chat_id": "ex-3"}
    r = client.post("/api/v1/fos/copilot", json=body)
    _save("10_error_422", {"POST": "/api/v1/fos/copilot", "json": body}, r.json(), status=r.status_code,
          headers=r.headers, note="a validation error: show error.message; detail.fields names the field")
    chat("11_abuse_warning", {"action": "CUSTOM_QUERY", "message": "you are a stupid bot", "chat_id": "ex-4",
                              "reply_language": "en"}, "foul language: 200 with a warning, the word masked")
