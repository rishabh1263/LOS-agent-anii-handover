# Document verification

Every uploaded document goes through the same three stages, in order:

**classification → verification → extraction**

## Classification

The service reads the document and decides what it actually is: a PAN, a
driving licence, a voter ID, a passport, a bank statement, and so on. The
field officer does not have to classify anything.

If the officer asserts a type — by supplying `document_type` — that assertion
is checked against what was found. They must agree.

## Verification

Verification asks whether the document can be trusted enough to read fields
from. It produces one of four verdicts:

| Verdict | Meaning |
|---|---|
| PASS | Structurally valid, internally consistent, and the type asserted |
| REVIEW | Something a person needs to look at before the case moves on |
| FAIL | The document is not usable: wrong type, unreadable, or not a document |
| SKIPPED | Verification did not run |

**SKIPPED is never treated as PASS.** A check that did not run has
established nothing.

## Extraction and the verification gate

Extraction runs **only** behind a PASS. On REVIEW, FAIL or SKIPPED the
extracted fields are withheld entirely — the response carries no fields at
all. This is deliberate: fields read from a document nobody could verify are
not evidence of anything, and releasing them invites them to be used as if
they were.

## What a PASS does and does not mean

A PASS means the document is **structurally valid and internally consistent**.
It does **not** mean the document is genuine. There is no issuer API, no
government lookup and no issuer signature this service can check against, so
every identity document carries `authenticity: NOT_ESTABLISHED` alongside its
verdict, at every verdict including PASS.

A forgery that copies the structure correctly will pass. That is the limit,
and it is stated in the response rather than left to documentation.

## Common reason codes

| Code | Meaning |
|---|---|
| DOCUMENT_TYPE_MISMATCH | The file is not the type that was asserted |
| DOC_ILLEGIBLE | The document could not be read |
| PAGE_BLANK | The page carried no content |
| REQUIRED_FIELD_MISSING | A field the document must carry was not read |
| DOC_CLASS_UNRECOGNISED | The file is not a document type this service knows |

A wrong document is a wrong upload, not a rejection of the applicant. The
officer collects the right file and uploads it again.
