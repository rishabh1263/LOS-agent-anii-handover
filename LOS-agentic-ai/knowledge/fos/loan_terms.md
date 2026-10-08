---
id: fos.loan_terms
knowledge_type: HANDBOOK
title: Common lending terms -- what a field officer is asked
description: Standard meanings of everyday lending terms (co-applicant, guarantor, sanction, disbursement, tenure, prepayment, NACH, CKYC, CERSAI, field checks). Company-specific charges are not configured here.
domain: general
language: en
source: LOS curated knowledge (this repository) -- general lending practice; company terms are in the product policy and the sanction letter
derived_from:
- app/config/stage_lifecycle.yaml
- app/config/eligibility_policy.yaml
- app/config/documents.yaml
related:
- fos.fos_workflow
- fos.eligibility_terms
- fos.cibil
---
# Common lending terms

These are the standard meanings used across Indian lenders. Where a term depends on the company's own terms
(a fee amount, a charge, a lock-in), that value is **not configured in this system**: it is in the product
policy and in the customer's sanction letter.

## What is an EMI bounce?

An **EMI bounce** is an EMI that could not be collected on its due date -- usually because the account did not have
enough money, or the NACH mandate failed. A bounce usually attracts a bounce charge and can hurt the customer's
credit record. The bounce charge amount is not configured in this system; it is in the sanction letter.

## What is a co-applicant?

A **co-applicant** applies for the loan together with the main applicant. A co-applicant's income can be counted
for eligibility, and a co-applicant is equally responsible for repaying the loan. A co-applicant is usually a
spouse or a close family member; for a property loan, every co-owner of the property is normally a co-applicant.
In this system a co-applicant has their own documents and their own KYC.

## What is a guarantor?

A **guarantor** is not a borrower. A guarantor promises to repay the loan if the borrower does not. A guarantor's
income is usually not counted for eligibility, but the guarantor's credit record is checked.

## What is the difference between a co-applicant and a guarantor?

A **co-applicant** borrows together with the applicant and shares the repayment from the start; their income can
add to eligibility. A **guarantor** does not borrow and pays only if the borrower defaults; their income does not
add to eligibility.

## What is a sanction letter?

A **sanction letter** is the lender's written approval of a loan. It states the approved amount, the interest
rate, the tenure, the EMI and the conditions to be met before disbursement. It is issued after the credit
decision, so it comes after the Credit stage in this system, never at FOS.

## What is disbursement?

**Disbursement** is the release of the loan money -- to the borrower, or for a home loan usually to the seller or
builder. It happens after sanction and after the loan documents are signed. In this system DISBURSEMENT is the
last stage: FOS, CPA, CREDIT, RCU, BOPS, HOPS, then DISBURSEMENT.

## What is loan tenure?

**Tenure** is the repayment period of the loan, in months or years. A longer tenure means a lower EMI but more
interest paid in total; a shorter tenure means a higher EMI and less interest. The configured minimum and maximum
tenure per product are in the eligibility policy (ask "maximum tenure").

## What is a processing fee?

A **processing fee** is a one-time charge for processing a loan application, usually a percentage of the loan
amount and often deducted from the amount disbursed. The processing fee amount is **not configured** in this
system; it is stated in the sanction letter.

## What is prepayment or part payment?

A **prepayment** (or **part payment**) is paying back part of the loan principal before it is due. It reduces the
outstanding amount, so either the EMI or the remaining tenure comes down. Any charge for it depends on the loan's
terms in the sanction letter; it is not configured in this system.

## What is foreclosure?

**Foreclosure** (or pre-closure) is repaying the whole outstanding loan early and closing it. Any foreclosure
charge depends on the loan's terms in the sanction letter; it is not configured in this system.

## What is a balance transfer?

A **balance transfer** (BT) moves an existing loan from another lender to this lender, usually for a lower interest
rate or better terms. The new lender pays off the old loan and the customer repays the new lender.

## What is a top-up loan?

A **top-up loan** is an extra loan given on top of an existing loan, often to a customer with a good repayment
record or together with a balance transfer. It is repaid with or alongside the existing loan.

## What is a loan against property?

A **loan against property** (LAP) is a secured loan given against a residential or commercial property the
borrower already owns; the property is the security. LAP is **not one of the products configured in this
system** -- the configured products are Personal Loan and Home Loan.

## What is NACH or e-mandate?

**NACH** (National Automated Clearing House, run by NPCI) lets the lender collect each EMI automatically from the
borrower's bank account, under a mandate the borrower signs. An **e-mandate** (e-NACH) is the same mandate given
digitally, through net banking, a debit card or Aadhaar.

## What is CKYC?

**CKYC** is the Central KYC Records Registry. A person's KYC record is stored once, centrally, and given a 14-digit
KYC Identifier; a regulated lender can use it to fetch the record instead of collecting the same documents again.
In this system KYC is checked against the documents uploaded for the case.

## What is video KYC?

Video KYC (V-CIP, video-based customer identification) is KYC done over a live, consent-based video call with
the lender's official, where the customer shows the original documents. It is a way to complete KYC without a
branch visit. In this system KYC compares the application form with the uploaded documents.

## What is CERSAI?

**CERSAI** (Central Registry of Securitisation Asset Reconstruction and Security Interest of India) is the
registry where a lender records its charge on a property given as security, so the same property cannot be
pledged to two lenders without it showing. It also holds the Central KYC records.

## What is a field investigation?

A **field investigation** (FI) is a visit by the lender or its agency to the customer's residence or office to
confirm the address and that the customer lives or works there.

## What is a personal discussion?

A **personal discussion** (PD) is an interview of the customer by the lender to understand their income, business
or job, existing loans and the purpose of the loan.

## What is legal verification?

**Legal verification** is a lawyer's check of a property's title documents, to confirm the seller or owner has a
clear title and that the property can be taken as security.

## What is technical valuation?

**Technical valuation** is an inspection of the property by an engineer or valuer, who confirms its condition and
estimates its market value. The value is used for the LTV (loan to value).

## What is Form 16?

**Form 16** is the certificate an employer gives each employee every year, showing the salary paid and the tax
(TDS) deducted. In this system a Form 16 is accepted as income proof, like a salary slip or an ITR.

## What is net salary?

**Net salary** (net pay) is the take-home pay after deductions such as tax and provident fund; **gross salary** is
the pay before deductions. This system's eligibility uses the verified **net pay**, not the gross salary.

## What is the credit stage?

The **Credit** stage comes right after CPA. The credit team assesses the customer's repayment capacity, credit
bureau record and the product policy, and decides on the loan. After Credit comes RCU, then BOPS, HOPS and
disbursement. Credit decisions are never made at FOS or in this chat.
