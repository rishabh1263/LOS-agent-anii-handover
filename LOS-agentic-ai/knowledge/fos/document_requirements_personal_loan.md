---
id: fos.document_requirements.personal_loan
knowledge_type: HANDBOOK
title: Personal loan document checklist
description: The documents a personal loan case needs at FOS - base, optional, and what the policy can add.
domain: documents
language: en
applies_to: [PERSONAL_LOAN]
source: LOS curated knowledge (this repository)
derived_from: [app/config/policies/personal_loan.yaml, app/config/applicant_agent.yaml]
policy_status: UNCONFIRMED
related: [fos.document_requirements, fos.document_policy, fos.address_proof]
---
# Personal loan document checklist

The personal loan checklist is resolved by the document policy engine from
`app/config/policies/personal_loan.yaml`, not from a fixed list. Every
application needs the base documents; a larger loan or a particular
employment type can add more.

## Personal loan base documents

Mandatory (the base), for every personal loan whatever the amount: PAN, ADDRESS_PROOF and
BANK_STATEMENT.

- **PAN** - satisfied by a PAN card
- **ADDRESS_PROOF** - satisfied by a driving licence, a passport or a voter ID
- **BANK_STATEMENT** - satisfied by a bank statement

Optional:

- **SIGNATURE** - a photo of the applicant's signature, checked by signature
  verification. Without a reference specimen it is REVIEW, never PASS.

## What the policy can add to a personal loan

Additional slots the policy can add:

- **INCOME_PROOF** - satisfied by a salary slip, an ITR or a Form 16
- **EMPLOYMENT_PROOF**

Which of those apply to a given case depends on the loan amount and on the
applicant's employment type. The case's own response says which rules
applied, in the `policy.applied_rules` field, and every checklist row names
the rule that put it there in `rule_ids`.

The policy file is UNCONFIRMED: its thresholds are placeholders no lender has
signed off, and every row derived from it carries `policy_status: UNCONFIRMED`.
