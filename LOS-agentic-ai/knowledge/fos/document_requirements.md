---
id: fos.document_requirements
knowledge_type: HANDBOOK
title: Document requirements
description: How a product's document checklist is defined - slots, the documents that satisfy them, and where the list comes from.
domain: documents
language: en
source: LOS curated knowledge (this repository)
derived_from: [app/config/applicant_agent.yaml, app/config/policies/personal_loan.yaml]
policy_status: UNCONFIRMED
related: [fos.document_requirements.personal_loan, fos.document_requirements.home_loan, fos.document_policy, fos.document_statuses, fos.address_proof]
---
# Document requirements

The documents a case requires depend on the loan product. The list is
configuration, not code, and it can change without a release.

Each product's list is its own knowledge item: the personal loan checklist
and the home loan checklist. What each checklist status means is described
once, in the statuses item; only VERIFIED satisfies a slot.

## Slots and document types

A checklist entry is a **slot**, not a document type. A slot names a
requirement; one or more document types satisfy it. ADDRESS_PROOF is the clear
case: there is no document called an address proof, but a driving licence, a
passport and a voter ID each prove an address.

Uploading a document against a slot name is allowed. The service resolves the
slot to the document types it accepts and checks the uploaded file against
that set.

An optional document never blocks the handoff to CPA. It appears on the
checklist so a field officer can see what has been collected beyond the
minimum, but its absence is not a pending item.

## Where this list comes from

The requirements are **this service's configuration** and nothing else: the
personal loan checklist is resolved by the document policy engine from
`app/config/policies/personal_loan.yaml`; every other product's checklist is
read from `app/config/applicant_agent.yaml`. They are not a copy of any
lender's published policy and they are not an industry standard.

Indian lenders commonly ask for more than this at the personal-loan stage —
Aadhaar, Form 16, ITR, employment proof and photographs all appear on public
lender checklists. **None of them is mandatory here**, because a document
becomes required only by being listed as a mandatory slot for a product in
the configuration.

The document taxonomy is deliberately wider than the checklist: this service
can classify and verify more types than any product currently requires, so a
product can begin requiring one by configuration change rather than a
release. FOS readiness depends on the configured checklist and on nothing
else.

If your organisation's policy differs, change the configuration. Do not read
this page as the policy itself.
