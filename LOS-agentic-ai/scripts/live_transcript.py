"""
The owner's 14-message transcript through the REAL HTTP API (a running uvicorn), not the test client.

    set LOS_BASE=http://127.0.0.1:8010
    set LOS_USER=<dev username>   &   set LOS_PASS=<dev password>     (or set LOS_TOKEN=<a bearer token>)
    python scripts/live_transcript.py > runs/live_transcript.md

Logs in with stage=FOS (so "what is my stage" reads the signed claim), then sends each message to /fos/copilot in
one chat and prints the markdown replies. Nothing is created: the officer's existing cases are used.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.environ.get("LOS_BASE", "http://127.0.0.1:8010").rstrip("/")
MESSAGES = ["what can you help with?", "ok", "mera cases kya hai", "what is CPA", "how to move cpa",
            "mandatory documents for Home loan", "process to verify documents", "what is my role", "what is my stage",
            "WHAT IS NAME", "FNR meaning", "why you are not answering me", "kyc status", "1"]


def post(path: str, body: dict, token: str | None = None) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    token = os.environ.get("LOS_TOKEN")
    if not token:
        user, password = os.environ.get("LOS_USER"), os.environ.get("LOS_PASS")
        if not user or not password:
            print("Set LOS_TOKEN, or LOS_USER and LOS_PASS.", file=sys.stderr)
            return 2
        token = post("/api/v1/auth/login", {"username": user, "password": password, "stage": "FOS"})["access_token"]
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"# Live transcript ({BASE}, /api/v1/fos/copilot, one chat)\n")
    for message in MESSAGES:
        reply = post("/api/v1/fos/copilot", {"action": "CUSTOM_QUERY", "message": message, "reply_language": "en",
                                             "chat_id": "live-transcript"}, token)
        print(f"## > {message}\n\n{reply.get('markdown', reply)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
