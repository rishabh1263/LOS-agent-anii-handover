---
id: fos.cpa_readiness
knowledge_type: HANDBOOK
title: CPA readiness
description: What READY_FOR_CPA means and does not mean, what blocks it, and the next action.
domain: readiness
language: en
source: LOS curated knowledge (this repository)
derived_from:
- app/config/stage_gates.yaml
- app/config/queries.yaml
policy_status: UNCONFIRMED
related:
- fos.fos_workflow
- fos.document_statuses
---
# CPA readiness

READY_FOR_CPA means the FOS stage has collected everything it is responsible
for and the case can be handed to the CPA desk.

## What it does NOT mean

READY_FOR_CPA is **not**:

- loan approval
- credit approval
- underwriting approval
- risk clearance
- a lending decision of any kind

It says only that the next desk has enough to start work. Nothing about
whether the loan should be granted has been decided, or even considered, at
this stage.

## What makes a case ready

A case is READY_FOR_CPA when there are no pending items. Concretely:

1. The applicant record carries every required field — full name, mobile,
   date of birth and address.
2. The application carries its required fields, including the product.
3. Every **mandatory** checklist slot is satisfied by a VERIFIED document.
4. No query or deviation raised on the case is still open. Both block the
   move out of the stage (`blocks_forward_move` in `app/config/queries.yaml`,
   UNCONFIRMED until the business signs it off).

Optional slots are ignored. A document under REVIEW blocks the handoff while
`block_on_review` is on, because a verdict nobody has resolved is not a
verdict the next desk can rely on.

## Why a case is not ready

The `pending_items` list gives the reasons, in the order a field officer
should work through them: applicant information first, then application
information, then documents. Information comes first because a document
collected against a half-captured applicant often has to be collected again.

Each pending item carries a code:

| Code | Meaning |
|---|---|
| MISSING_FULL_NAME, MISSING_MOBILE, MISSING_DATE_OF_BIRTH, MISSING_ADDRESS | An applicant field is not captured |
| MISSING_PRODUCT | The application has no loan product |
| DOCUMENT_MISSING | A mandatory slot has nothing uploaded |
| DOCUMENT_REJECTED | A document failed and must be replaced |
| DOCUMENT_UNDER_REVIEW | A document needs a person to resolve it |
| DOCUMENT_NOT_VERIFIED | A document has not finished verification |

## The next action

`next_action` is the single thing to do now — not a list. The full list is in
`pending_items`. The action is the first outstanding item in working order,
so a field officer clearing a queue can act on it without reading everything.

Possible actions: CREATE_APPLICANT, CREATE_APPLICATION,
CAPTURE_APPLICANT_INFORMATION, CAPTURE_APPLICATION_INFORMATION,
COLLECT_DOCUMENT, REQUEST_CORRECT_DOCUMENT, RESOLVE_DOCUMENT_REVIEW,
AWAIT_VERIFICATION, and SUBMIT_TO_CPA once nothing is outstanding.
