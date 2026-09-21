# Statuses, and what each one means

There are four different status fields on a case, and they answer different
questions. Confusing them is the most common misreading of a response.

## Document status

The state of one uploaded file:

- **UPLOADED** — held, not yet processed
- **PROCESSING** — verification is running
- **VERIFIED** — passed verification
- **REVIEW** — needs a person
- **REJECTED** — failed verification

## Verification verdict

What verification decided about the document: PASS, REVIEW, FAIL or SKIPPED.

A document can report status REJECTED with verdict FAIL, or status REVIEW
with verdict REVIEW. The two are related but not the same field: a document
that could not be processed at all reports FAILED as its status while its
verification is SKIPPED, and without both fields those are indistinguishable
from verification having been switched off.

## Checklist slot status

The state of a **requirement**, not a file. A slot shows the best outcome
among the documents uploaded against it: a rejected first attempt followed by
a verified re-upload is a satisfied slot, not a blocked one.

Only VERIFIED satisfies a slot. MISSING, UPLOADED, PROCESSING, REVIEW and
REJECTED all leave it outstanding.

## Case stage

Where the whole case is: APPLICATION_CREATED, DOCUMENT_COLLECTION,
BASIC_DOCUMENT_VERIFICATION or READY_FOR_CPA.

## Reading them together

A case can be in BASIC_DOCUMENT_VERIFICATION with one VERIFIED document, one
REJECTED document and one slot still MISSING. That is normal mid-collection
state, and `next_action` says which of those to deal with first.

## What SKIPPED means, everywhere it appears

SKIPPED always means "this was not done", never "this was done and found
nothing wrong". On a verification verdict it means verification did not run.
On a KYC field it means there was nothing to compare — either no document
carried the field, or only one did, and one document cannot corroborate
itself. A SKIPPED KYC field reports `match_score: 0`, and that zero means
nothing was compared, not that nothing matched.

SKIPPED is never treated as a pass.
