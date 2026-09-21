"""
Mint a LOCAL DEVELOPMENT FOS token for Swagger testing.

    python make_fos_token.py                 # the standard FOS scope set
    python make_fos_token.py --read-only     # reads only, no writes
    python make_fos_token.py --scopes "read_applicant read_documents"
    python make_fos_token.py --ttl 8         # hours, default 2
    python make_fos_token.py --claims        # show the claims, NOT the token

The token is printed to stdout and nowhere else. It is never written to a
file, and this script never logs it -- a development credential that lands in
a file gets committed eventually.

DEVELOPMENT ONLY. It signs with the local keypair that auth_provider_dev.py
generates. A real deployment gets its tokens from the identity provider named
by JWT_JWKS_URL, and this script cannot mint anything that provider would
accept.

Requires auth_provider_dev.py to have been run once, which is what creates
auth_keys/jwt_signing_private.pem.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

BASE_DIR = Path(__file__).resolve().parent
PRIVATE_KEY = BASE_DIR / "auth_keys" / "jwt_signing_private.pem"

# Everything a field officer needs to use the copilot: read every part of the
# FOS-stage picture, and make the record changes the FOS stage owns.
FOS_SCOPES = [
    "read_applicant",
    "read_application",
    "read_documents",
    "read_verification",
    "read_pending_items",
    "read_next_action",
    "create_applicant",
    "update_applicant",
    "create_application",
    "upload_document",
    "modify_application",
]

# Reads only. Useful for checking that the write path is genuinely refused
# rather than merely untested.
READ_ONLY_SCOPES = [s for s in FOS_SCOPES if s.startswith("read_")]


def build(scopes: list[str], ttl_hours: float, subject: str) -> tuple[str, dict]:
    if not PRIVATE_KEY.exists():
        raise SystemExit(
            f"{PRIVATE_KEY} not found.\n"
            "Run `python auth_provider_dev.py` once to generate the local "
            "development keypair."
        )

    now = datetime.now(timezone.utc)
    claims = {
        "sub": subject,
        "iss": "los-local",
        "aud": "los-agentic-ai",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(hours=ttl_hours),
        "jti": f"fos-{int(now.timestamp())}",
        "scope": " ".join(scopes),
        "role": "fos",
    }
    token = jwt.encode(
        claims,
        PRIVATE_KEY.read_text(encoding="utf-8"),
        algorithm="RS256",
        headers={"kid": "los-rs256-1"},
    )
    return token, claims


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mint a local development FOS token for Swagger testing.",
    )
    parser.add_argument("--scopes", help="Space-separated scopes to use instead "
                                         "of the standard FOS set.")
    parser.add_argument("--read-only", action="store_true",
                        help="Reads only, so write refusals can be tested.")
    parser.add_argument("--ttl", type=float, default=2.0,
                        help="Lifetime in hours (default 2).")
    parser.add_argument("--subject", default="fos-dev-user",
                        help="The `sub` claim (default fos-dev-user).")
    parser.add_argument("--claims", action="store_true",
                        help="Print the claims and scope list instead of the "
                             "token. Nothing secret is shown.")
    args = parser.parse_args()

    if args.scopes:
        scopes = args.scopes.split()
    elif args.read_only:
        scopes = READ_ONLY_SCOPES
    else:
        scopes = FOS_SCOPES

    token, claims = build(scopes, args.ttl, args.subject)

    if args.claims:
        # Deliberately not the token. This mode exists so the scope set can be
        # checked, pasted into a ticket or diffed without the credential
        # travelling with it.
        print(f"subject : {claims['sub']}")
        print(f"issuer  : {claims['iss']}")
        print(f"audience: {claims['aud']}")
        print(f"expires : {claims['exp'].isoformat()}")
        print(f"scopes  : {len(scopes)}")
        for scope in scopes:
            print(f"  - {scope}")
        print("\n(token not shown; run without --claims to print it)")
        return 0

    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
