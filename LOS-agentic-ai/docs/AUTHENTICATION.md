# Authentication

How LOS Agentic AI authenticates callers, what it validates, and what it
deliberately does not do.

This document describes the implementation in `app/security/auth.py`. No token
appears in this repository or in this document.

---

## 1. The model

**LOS Agentic AI is a resource server, not an identity provider.**

It validates short-lived RS256 access tokens issued by *your* identity
provider and resolves the signing keys from that provider's JWKS endpoint.

It:

- validates short-lived access JWTs
- resolves signing keys from JWKS, selected by the token's `kid`
- validates signature, `exp`, `nbf`, `iat`, `iss` and `aud`
- supports scope and role authorization
- **never** issues a token
- **never** stores an access token
- **never** stores a refresh token
- **never** holds the identity provider's private signing key

A compromise of this service therefore cannot mint tokens for itself or for
anything else.

---

## 2. The header

Every business endpoint requires:

```
Authorization: Bearer <access_token>
```

The scheme comparison is case-insensitive; anything other than `Bearer` is
rejected with `401`.

---

## 3. What is validated, in order

| Step | Check | Failure |
|---|---|---|
| 1 | `Authorization` header present | `401` `Missing Bearer token.` |
| 2 | Scheme is `Bearer` | `401` `Authorization scheme must be Bearer.` |
| 3 | Header parses as a JWT | `401` `Invalid JWT header.` |
| 4 | `alg` equals `JWT_ALGORITHM` | `401` `JWT algorithm is not allowed.` |
| 5 | `kid` present | `401` `JWT kid is missing.` |
| 6 | `kid` resolves against JWKS | `401` `Unable to resolve JWT signing key.` |
| 7 | Signature verifies | `401` `JWT signature is invalid.` |
| 8 | `exp` in the future | `401` `JWT token expired.` |
| 9 | `nbf` reached | `401` `JWT token is not active yet.` |
| 10 | `iat` sane | `401` `JWT issued-at claim is invalid.` |
| 11 | `iss` matches `JWT_ISSUER` | `401` `JWT issuer is invalid.` |
| 12 | `aud` matches `JWT_AUDIENCE` | `401` `JWT audience is invalid.` |

Signature verification is never skipped, and no environment variable turns
authentication off. There is no `API_AUTH_ENABLED=false` bypass in the current
code — the older `verify_api_key` name survives only as an alias that calls
`require_jwt`, and no `X-API-Key` path remains.

### Algorithm allow-list

```
RS256  RS384  RS512  ES256  ES384  ES512
```

**Asymmetric only.** `HS256` and `none` are not in the list, so a token cannot
be re-signed with a key this service publishes, and an `alg: none` token is
rejected at step 4 before any key is resolved.

The token's `alg` must equal the *configured* `JWT_ALGORITHM` exactly. Being
in the allow-list is not sufficient — this prevents an algorithm-substitution
attack even between two otherwise acceptable algorithms.

---

## 4. Configuration

All six are read once at import. The first four are **required**:
`validate_auth_configuration()` raises at startup if `JWT_JWKS_URL`,
`JWT_ISSUER` or `JWT_AUDIENCE` is empty, or if `JWT_ALGORITHM` is not in the
allow-list.

| Variable | Default | Meaning |
|---|---|---|
| `JWT_JWKS_URL` | *(none — required)* | Your identity provider's JWKS endpoint. Public keys are fetched from here by `kid`. |
| `JWT_ISSUER` | *(none — required)* | Expected `iss` claim. |
| `JWT_AUDIENCE` | *(none — required)* | Expected `aud` claim. |
| `JWT_ALGORITHM` | `RS256` | Expected `alg`. Must be in the allow-list above. |
| `JWT_LEEWAY_SECONDS` | `30` | Clock-skew tolerance applied to `exp`, `nbf` and `iat`. Floor 0. |
| `JWT_JWKS_CACHE_SECONDS` | `300` | How long a fetched JWKS is cached. Floor 30. |

Example values (placeholders — substitute your provider's):

```bash
JWT_JWKS_URL=https://idp.example.internal/.well-known/jwks.json
JWT_ISSUER=https://idp.example.internal/
JWT_AUDIENCE=los-agentic-ai
JWT_ALGORITHM=RS256
JWT_LEEWAY_SECONDS=30
JWT_JWKS_CACHE_SECONDS=300
```

### Key rotation

Keys are cached for `JWT_JWKS_CACHE_SECONDS` and selected per-token by `kid`,
so rotation needs no restart: publish the new key in your JWKS, and tokens
signed with it validate once the cache expires. Keep the retiring key
published for at least one cache period plus the longest token lifetime.

---

## 5. Which endpoints require a token

**Authenticated** — every business endpoint, mounted under `/api/v1` with
`dependencies=[Depends(require_jwt)]`:

```
POST /api/v1/los/process              <- the canonical endpoint
POST /api/v1/document-agent
POST /api/v1/verify
POST /api/v1/extract-document
POST /api/v1/extract-document/batch
POST /api/v1/financial/verify
POST /api/v1/financial/extract
POST /api/v1/kyc
POST /api/v1/agents/execute
GET  /api/v1/agents/
GET  /api/v1/extract-document/supported
GET  /api/v1/financial/supported
```

**Unauthenticated** — infrastructure probes only, deliberately, so a load
balancer does not need a credential:

```
GET /health     liveness
GET /ready      readiness
GET /metrics    metrics
```

None of the three returns applicant data. If your network exposes this service
publicly, restrict `/metrics` at the proxy.

---

## 6. Scopes and roles

Scope and role dependencies exist and are available, but **the routers are
mounted with `require_jwt` only** — a valid token is currently sufficient for
every business endpoint. Fine-grained authorization is opt-in per route.

Available helpers in `app/security/auth.py`:

| Helper | Effect |
|---|---|
| `require_scope("los.write")` | `403` unless that scope is present |
| `require_any_scope([...])` | `403` unless at least one is present |
| `require_all_scopes([...])` | `403` unless every one is present, naming the missing ones |
| `require_role("los-service")` | `403` unless that role is present |
| `require_any_role([...])` | `403` unless at least one is present |

Scopes are read from `scope` (space-delimited string) or `scp`; roles from
`roles` or `role` (list, or comma/space-delimited string). Both forms are
accepted so a token from either a typical OAuth2 or a typical OIDC provider
works unchanged.

Claim accessors: `get_subject`, `get_client_id` (`client_id`, `azp` or
`appid`), `get_scopes`, `get_roles`.

To require a scope on a route:

```python
from fastapi import Security
from app.security.auth import require_scope

@router.post("/something")
async def handler(claims: dict = Security(require_scope("los.write"))):
    ...
```

---

## 7. Failure responses

`401` responses carry `WWW-Authenticate: Bearer`. `403` responses do not — the
credential was accepted, it simply was not sufficient.

```json
{ "detail": "Missing Bearer token." }
```

The message names the *class* of failure, never the token, the claim values,
or the expected issuer/audience.

---

## 8. Local development

`auth_provider_dev.py` is a **development-only** identity provider: it
generates an RSA keypair on first run, serves a JWKS, and issues tokens via
OAuth2 client credentials.

**It is not part of the deployable service.** It is not included in the
handover archive, it must never run in a shared or production environment, and
its default client secret is a placeholder.

```bash
# terminal 1 — dev identity provider on :8020
JWT_ISSUER=los-local JWT_AUDIENCE=los-agentic-ai python auth_provider_dev.py

# terminal 2 — the service on :8010
python -m uvicorn main:app --host 127.0.0.1 --port 8010
```

Get a token (note the value is captured to a variable, not echoed):

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8020/oauth/token \
  -d grant_type=client_credentials \
  -d client_id=los-demo-client \
  -d client_secret="$AUTH_CLIENT_SECRET" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s -X POST http://127.0.0.1:8010/api/v1/los/process \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@pan.jpg" -F "operation=PROCESS"
```

### Using Swagger

Open `http://127.0.0.1:8010/docs`, click **Authorize**, and paste the raw token
(Swagger adds the `Bearer ` prefix itself). Every `/api/v1` operation is then
callable from the page, including the multi-file upload on
`POST /api/v1/los/process`.

---

## 9. For production

- Treat tokens as **short-lived**. This service caches nothing about them and
  re-validates every request, so a short `exp` costs it nothing.
- Serve `JWT_JWKS_URL` over **HTTPS**. It is fetched over plain HTTP if you
  configure it that way, which would let a network attacker substitute keys.
- Keep the private signing key in the identity provider. It is never needed
  here, and there is no code path that would read one.
- Do not weaken the algorithm allow-list to admit `HS256`.
- Never commit a `.env`. Use `.env.example` as the template — it contains
  placeholders only.
