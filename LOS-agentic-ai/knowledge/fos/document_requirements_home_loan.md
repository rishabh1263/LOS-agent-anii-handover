---
id: fos.document_requirements.home_loan
knowledge_type: HANDBOOK
title: Home loan document checklist
description: The documents a home loan case needs at FOS - mandatory and optional.
domain: documents
language: en
applies_to: [HOME_LOAN]
source: LOS curated knowledge (this repository)
derived_from: [app/config/applicant_agent.yaml]
related: [fos.document_requirements, fos.address_proof]
---
# Home loan document checklist

The home loan checklist is read from the `HOME_LOAN` entry of
`app/config/applicant_agent.yaml`.

## Home loan mandatory and optional documents

Mandatory, for every home loan: PAN, BANK_STATEMENT and ADDRESS_PROOF.

- **PAN** - satisfied by a PAN card
- **BANK_STATEMENT** - satisfied by a bank statement
- **ADDRESS_PROOF** - satisfied by a driving licence, a passport or a voter ID

Optional: ITR, SALARY_SLIP, EMPLOYMENT_PROOF and SIGNATURE. An optional
document is shown on the checklist when collected but never blocks the
handoff to CPA.
