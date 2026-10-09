---
id: fos.officer_howto
knowledge_type: HANDBOOK
title: What to do when -- a field officer's quick answers
description: Who approves a loan, the stages of the loan, fixing a rejected document, a failed KYC, and the bank statement period.
domain: general
language: en
source: LOS curated knowledge (this repository)
derived_from:
- app/config/stage_lifecycle.yaml
- app/config/applicant_agent.yaml
- app/config/documents.yaml
related:
- fos.fos_workflow
- fos.document_statuses
- fos.cpa_readiness
---
# What to do when

## Who approves or decides on the loan?

The **credit team** approves or rejects a loan, at the **Credit** stage after CPA. The field officer (FOS) and
this chat never approve, reject or sanction a loan; FOS makes the case complete and moves it to CPA.

## What are the stages of a loan?

Every case moves through these stages, in this order: **FOS** (details and documents collected) → **CPA** →
**Credit** (the credit decision) → **RCU** (risk check) → **BOPS** → **HOPS** → **Disbursement** (the money is
released).

## How do I fix a rejected document?

Open the case and upload a **correct, clear** copy of the same document again. The new upload replaces the
rejected one for that slot; it is verified again, and the case moves forward once it passes. The reason the
document was rejected is shown in the case, so fix that first (wrong document type, unreadable, cropped, details
not matching the form).

## What to do if KYC fails?

A failed KYC means the documents do not match the application form, or do not match each other. Open the case to
see which detail did not match, then ask the customer to upload correct documents that match the application form.
KYC runs again on the new documents. A case with a failed KYC is not ready for CPA.

## How many months of bank statement are needed?

The checklist asks for a **bank statement**; the number of months is **not configured** in this system. The
product policy of the credit team decides the period -- check it before collecting the statement.

## How does KYC work?

KYC is checked for every party (the applicant and each co-applicant) in two ways: **A** -- the application form
against the documents (name, date of birth, PAN, address match what was declared), and **B** -- the documents
against each other (the same person on every document). The result is pass, review (a person checks) or failed.
KYC runs once the identity documents are uploaded; a failed KYC must be fixed before the case can move to CPA.

## How do I add a co-applicant?

Open the case and upload the co-applicant's documents, choosing **Co-applicant** as the party when asked. The
co-applicant's documents and KYC are checked separately from the applicant's.

## What happens after CPA?

After CPA the file goes to the **Credit** stage, which assesses the case and makes the credit decision, then **RCU**
checks the file for risk, then **BOPS** and **HOPS**, and finally the loan is **disbursed**. At CPA itself the
completed file is processed further and handed on. FOS does not take part in these later stages.
