---
id: fos.eligibility_terms
knowledge_type: HANDBOOK
title: Eligibility terms -- FOIR, income basis and minimum income
description: What FOIR means, how it is calculated, and the eligibility limits configured for the products.
domain: eligibility
language: en
source: LOS curated knowledge (this repository)
derived_from:
- app/config/eligibility_policy.yaml
related:
- fos.cpa_readiness
- fos.document_requirements
---
# Eligibility terms

## What is FOIR?

**FOIR** stands for **Fixed Obligation to Income Ratio**. It measures how much of
an applicant's monthly income is already committed to fixed obligations, and
whether the new loan's EMI is affordable on top of them.

FOIR = (existing monthly EMIs and fixed obligations + the proposed loan's EMI)
÷ monthly income × 100.

A lower FOIR means more of the income is free; a higher FOIR means the applicant
is more stretched.

## The configured FOIR limit

For every product currently configured, the **maximum FOIR is 50 percent**. A
case above the limit is not rejected automatically: it is flagged
FOIR_ABOVE_THRESHOLD and goes to a reviewer to assess affordability. FOIR is an
essential rule: without the figures to calculate it, eligibility cannot be
assessed.

## Which income is used

Income is taken from the verified **net pay** (take-home pay on the salary slip),
not the gross salary. The configured **minimum monthly income is ₹25,000**; a
verified income below it is flagged INCOME_BELOW_MINIMUM for review.

## Who decides

Eligibility is evaluated from verified documents against the configured policy.
The FOS officer does not calculate or override it; flagged cases are reviewed
by the reviewer, and the lending decision is made downstream.
