---
id: fos.cibil
knowledge_type: HANDBOOK
title: CIBIL and credit scores
description: What CIBIL and a credit score are, and where they fit in this loan process.
domain: credit
language: en
source: LOS curated knowledge (this repository)
requires_flag: COPILOT_TERMS_KNOWLEDGE
derived_from:
- app/config/stage_lifecycle.yaml
related:
- fos.eligibility_terms
- fos.fos_workflow
---
# CIBIL and credit scores

This chatbot does not fetch CIBIL scores; the credit check happens at the Credit
stage, one of the later stages of this loan process.

## What is CIBIL?

**CIBIL** (TransUnion CIBIL) is one of India's **credit bureaus**. A credit bureau
collects how a person has handled loans and credit cards -- repayments, delays,
defaults, open accounts and recent loan enquiries -- from banks and lenders.

## What is a CIBIL score (credit score)?

A **credit score** is a three-digit number, from **300 to 900**, that a bureau
calculates from that history. A higher score means a better repayment record.

The score improves with on-time repayments and low use of available credit, and
falls with late payments, defaults and many loan applications in a short time.

## Approval thresholds

This chatbot cannot state the score needed for a loan approval. Whether an
application meets the lender's requirements is decided at the **Credit** stage,
one of the later stages, not at FOS.

## Where it fits in this process

The FOS stage collects documents and checks KYC; it does **not** fetch or decide
on a credit score. This system does not pull a bureau report. Creditworthiness,
including any bureau check, is assessed in the later stages, at the **Credit** stage.

So the assistant can explain what a credit score is, but it cannot tell you an
applicant's score.
