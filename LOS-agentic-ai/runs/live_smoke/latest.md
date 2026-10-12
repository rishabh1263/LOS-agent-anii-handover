# Live smoke 2026-10-12 17:22

## > show my cases
_HTTP 200, 586 ms_

You have 2 cases: 2 with documents **pending**.

1. Priya V. · CASE-80889FD152F6 · Created · 5 docs pending · [Open](action:open_case?id=CASE-80889FD152F6)
2. Rahul S. · CASE-AED785CD1CD1 · Created · 4 docs pending · [Open](action:open_case?id=CASE-AED785CD1CD1)

Showing 1-2 of 2 (newest first).

[Show as a table](ask:Show%20as%20a%20table)

Do you want to know about a particular case?
[Yes](ask:Yes) · [No](ask:No)

Download: [Excel](action:download_list?format=xlsx&q=all) · [Doc](action:download_list?format=docx&q=all) · [PDF](action:download_list?format=pdf&q=all) · [Show in UI](action:show_list_in_ui?q=all)

## > open CASE-AED785CD1CD1
_HTTP 200, 178 ms_

CASE-AED785CD1CD1 · Rahul Sharma · FOS (Field Officer Sales)
PAN (Permanent Account Number), Address Proof, Bank Statement and Signature are not uploaded, so Rahul Sharma's KYC (Know Your Customer) hasn't run yet.
Next step: Upload the PAN.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

[Close](action:exit_case)

## > kya pending hai
_HTTP 200, 2556 ms_

CASE-AED785CD1CD1
Still pending:
- **PAN** (Permanent Account Number)
- **Address Proof**
- Bank Statement
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

## > why?
_HTTP 200, 81 ms_

PAN, Address Proof, Bank Statement and Signature are not uploaded, so Rahul Sharma's KYC hasn't run yet.
Next step: Upload the PAN.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is pending?](ask:What%20is%20pending?)

[Close](action:exit_case)

## > where does this one stand right now
_HTTP 200, 366 ms_

Your application is in the FOS (Field Officer Sales) stage, at Application Created. Rahul Sharma — the application is at Application Created. **Pending**: PAN has not been uploaded. Address Proof has not been uploaded. Bank Statement has not been uploaded. Signature has not been uploaded. Next action: Collect and upload the missing document: PAN. CPA readiness: Not ready — 5 item(s) blocking.

**Next step:** Ask "what is **pending**" for this stage.

- [Why is PAN required for this application?](ask:Why%20is%20PAN%20required%20for%20this%20application?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

## > kitna loan maanga hai
_HTTP 200, 355 ms_

The loan amount on your application is **₹5,00,000**.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

## > who is the applicant
_HTTP 200, 318 ms_

The name on your application is Rahul Sharma.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

## > is this case ready for CPA?
_HTTP 200, 275 ms_

CPA readiness: 1 of 12 checks passed.

**Applicant documents**
- PAN: **PENDING**, not uploaded yet. Fix: Upload the PAN.
- Address Proof: **PENDING**, not uploaded yet. Fix: Upload the Address Proof.
- Bank Statement: **PENDING**, not uploaded yet. Fix: Upload the Bank Statement.
- Signature: **PENDING**, not uploaded yet. Fix: Upload the Signature.

**KYC**
- Applicant: name: **PENDING**. KYC has not run for this party. Fix: Upload the identity documents so KYC can run.
- Applicant: date of birth: **PENDING**. KYC has not run for this party. Fix: Upload the identity documents so KYC can run.
- Applicant: address: **PENDING**. KYC has not run for this party. Fix: Upload the identity documents so KYC can run.
- Applicant: PAN number: **PENDING**. KYC has not run for this party. Fix: Upload the identity documents so KYC can run.
- Applicant: father's name: **PENDING**. KYC has not run for this party. Fix: Upload the identity documents so KYC can run.
- Applicant: application name vs PAN: **PENDING**, no verified PAN for this party yet. Fix: Upload the identity documents so KYC can run.

**Next step:** Upload the PAN.

- [What should I do next?](ask:What%20should%20I%20do%20next?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

## > what about the co-applicant?
_HTTP 200, 51 ms_

There is no co-applicant on CASE-AED785CD1CD1, so nothing is **pending** for one.

[Close](action:exit_case)

## > is the customer eligible? how much can he borrow?
_HTTP 200, 224 ms_

Eligibility has not been evaluated for this case yet. It is assessed once the income evidence and the loan terms are in place.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is pending?](ask:What%20is%20pending?)

[Close](action:exit_case)

## > what does the credit memo say
_HTTP 200, 4299 ms_

Credit and bureau information is handled by the downstream Credit process.

**Next step:** Check your status or **pending** documents?

- [What is still pending on my case?](ask:What%20is%20still%20pending%20on%20my%20case?)
- [What stage is my application at?](ask:What%20stage%20is%20my%20application%20at?)

[Close](action:exit_case)

## > raise a query: bank statement for last 6 months needed
_HTTP 200, 92 ms_

Draft query on CASE-AED785CD1CD1 (to: the customer, type: Clarification). Confirm to raise it:

> bank statement for last 6 months needed

[Confirm](action:confirm_write?ref=free-011be4) · [Cancel](action:cancel_write?ref=free-011be4)

[Close](action:exit_case)

## > cancel
_HTTP 200, 479 ms_

Okay, nothing was sent or changed.

[Close](action:exit_case)

## > raise deviation for low cibil score
_HTTP 200, 181 ms_

Which deviation from the master? Pick one:
- [Negative Dedupe / CIBIL catch](ask:raise%20deviation%20DVM-43EAEF)

[Close](action:exit_case)

## > cancel
_HTTP 200, 605 ms_

Okay, leaving that. What would you like to know?

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

## > what is FOIR?
_HTTP 200, 374 ms_

FOIR (Fixed Obligation to Income Ratio): the share of monthly income already going to EMIs and other fixed payments.

[Close](action:exit_case)

## > what comes after CPA in the process?
_HTTP 200, 2802 ms_

Every case moves through these stages, in this order: FOS (details and documents collected) → CPA → Credit (the credit decision) → RCU (risk check) → BOPS → HOPS → Disbursement (the money is released).

**Next step:** Check your status or **pending** documents?

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

## > write me a poem
_HTTP 200, 414 ms_

That's outside what I can help with here. I'm set up for your loan application. I can check its status, documents, verification or KYC (Know Your Customer) for you.

- [What is my stage?](ask:What%20is%20my%20stage?)
- [What's pending?](ask:What's%20pending?)

[Close](action:exit_case)

## > close
_HTTP 200, 46 ms_

CASE-AED785CD1CD1 closed.

You have 2 cases: 2 with documents **pending**.

1. Priya V. · CASE-80889FD152F6 · Created · 5 docs pending · [Open](action:open_case?id=CASE-80889FD152F6)
2. Rahul S. · CASE-AED785CD1CD1 · Created · 4 docs pending · [Open](action:open_case?id=CASE-AED785CD1CD1)

Showing 1-2 of 2 (newest first).

[Show as a table](ask:Show%20as%20a%20table)

Do you want to know about a particular case?
[Yes](ask:Yes) · [No](ask:No)

Download: [Excel](action:download_list?format=xlsx&q=all) · [Doc](action:download_list?format=docx&q=all) · [PDF](action:download_list?format=pdf&q=all) · [Show in UI](action:show_list_in_ui?q=all)

## > which of my cases are pending for KYC?
_HTTP 200, 41 ms_

None of your cases match (KYC issue).

## > show me the next two
_HTTP 200, 63 ms_

None of your cases match (KYC issue).


**p50 318 ms, max 4299 ms, turns 21**
