from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

private_key_path = Path("auth_keys/jwt_signing_private.pem")

if not private_key_path.exists():
    raise SystemExit("jwt_signing_private.pem not found")

private_key = private_key_path.read_text(encoding="utf-8")

now = datetime.now(timezone.utc)

payload = {
    "sub": "los-local-demo-client",
    "iss": "los-local",
    "aud": "los-agentic-ai",
    "iat": now,
    "nbf": now,
    "exp": now + timedelta(hours=1),
    "jti": f"local-{int(now.timestamp())}",
    "scope": "los.read los.write",
    "role": "los-service",
}

token = jwt.encode(
    payload,
    private_key,
    algorithm="RS256",
    headers={"kid": "los-rs256-1"},
)

print(token)
