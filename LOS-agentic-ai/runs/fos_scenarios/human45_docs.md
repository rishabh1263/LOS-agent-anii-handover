# Human-style test, 45 questions (live :8010, FOS token, case CASE-B18F442C3777)

p50 2.21s · p95 5.54s · max 5.54s


## DOCS

### D1. > open CASE-B18F442C3777  (0.08s)

CASE-B18F442C3777 · E2E Applicant · FOS (Field Officer Sales)
Applicant: name **did not pass**: the documents disagree.
Next step: Upload the Address Proof.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

[Close](action:exit_case)

### D2. > docs  (0.12s)

What would you like to know about docs?

1. [What is pending?](ask:What%20is%20pending%3F)
2. [Why is this case stuck?](ask:Why%20is%20this%20case%20stuck%3F)
3. [What should I do next?](ask:What%20should%20I%20do%20next%3F)

[Close](action:exit_case)

### D3. > which documents are pending  (3.88s)

CASE-B18F442C3777
KYC (Know Your Customer) needs attention:
• **Name** mismatch: PAN (Permanent Account Number) shows "RISHABH AJIT SINGH", PAN shows "LAXMI SANTOSH GUPTA"
• **Date of birth** mismatch: PAN shows "2002-06-12", PAN shows "2004-12-20"
• PAN number mismatch: PAN shows "XXXXXX875K", PAN shows "XXXXXX189E"
• Father's name mismatch: PAN shows "AJIT SINGH", PAN shows "SANTOSH RAMASHARE GUPTA"
Other checks: Address not found on the documents
Please upload the correct documents:
- PAN
Still pending:
- Address Proof
- Bank Statement
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### D4. > what documents are needed for personal loan  (0.25s)

Required: PAN (Permanent Account Number), Address Proof (Driving Licence, Passport or Voter ID), Bank Statement, Signature.

### D5. > is signature mandatory  (2.62s)

No Signature document has been uploaded for this case. However, the application is **under review** because the name on the PAN, RISHABH AJIT SINGH, does not match the PAN name, LAXMI SANTOSH GUPTA.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### D6. > which address proofs are accepted  (0.09s)

Driving Licence, Passport or Voter ID can satisfy the Address Proof requirement.

### D7. > my PAN got rejected why  (0.34s)

PAN is **verified**. Its issuing authority has not been independently confirmed. However, the application is **under review** because the name on the PAN, RISHABH AJIT SINGH, does not match the PAN name, LAXMI SANTOSH GUPTA.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### D8. > do i need bank statement  (5.43s)

4 required: PAN — VERIFIED; Address Proof — MISSING (any one of: Driving Licence, Passport and Voter ID); Bank Statement — MISSING; Signature — MISSING. This list is not final: employment type has not been captured, so the rules that depend on it could not be applied.

- [What can be used as address proof?](ask:What%20can%20be%20used%20as%20address%20proof?)
- [Why is address proof required for this application?](ask:Why%20is%20address%20proof%20required%20for%20this%20application?)

[Close](action:exit_case)

### D9. > how many months bank statement  (3.29s)

No Bank Statement document has been uploaded for this case. However, the application is **under review** because the name on the PAN, RISHABH AJIT SINGH, does not match the PAN name, LAXMI SANTOSH GUPTA.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### D10. > co applicant documents kya chahiye  (1.82s)

There is no co-applicant on CASE-B18F442C3777, so nothing is **pending** for one.

[Close](action:exit_case)

### D11. > upload karna hai pan  (4.63s)

KYC needs attention:
• **Name** mismatch: PAN shows "RISHABH AJIT SINGH", PAN shows "LAXMI SANTOSH GUPTA"
• **Date of birth** mismatch: PAN shows "2002-06-12", PAN shows "2004-12-20"
• PAN number mismatch: PAN shows "XXXXXX875K", PAN shows "XXXXXX189E"
• Father's name mismatch: PAN shows "AJIT SINGH", PAN shows "SANTOSH RAMASHARE GUPTA"
Other checks: Address not found on the documents
Please upload the correct documents:
- PAN
Still pending:
- Address Proof
- Bank Statement
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### D12. > what is wrong with the uploaded pan  (0.33s)

PAN is **verified**. Its issuing authority has not been independently confirmed. However, the application is **under review** because the name on the PAN, RISHABH AJIT SINGH, does not match the PAN name, LAXMI SANTOSH GUPTA.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What can be used as address proof?](ask:What%20can%20be%20used%20as%20address%20proof?)

[Close](action:exit_case)

### D13. > can i upload aadhaar instead of pan  (2.21s)

Tap below to upload the PAN (Permanent Account Number).

[Upload PAN](action:upload?doc=PAN&party=applicant)

[Close](action:exit_case)

### D14. > which documents are under review  (2.88s)

KYC needs attention:
• **Name** mismatch: PAN shows "RISHABH AJIT SINGH", PAN shows "LAXMI SANTOSH GUPTA"
• **Date of birth** mismatch: PAN shows "2002-06-12", PAN shows "2004-12-20"
• PAN number mismatch: PAN shows "XXXXXX875K", PAN shows "XXXXXX189E"
• Father's name mismatch: PAN shows "AJIT SINGH", PAN shows "SANTOSH RAMASHARE GUPTA"
Other checks: Address not found on the documents
Please upload the correct documents:
- PAN
Still pending:
- Address Proof
- Bank Statement
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### D15. > re upload what  (5.54s)

KYC needs attention:
• **Name** mismatch: PAN shows "RISHABH AJIT SINGH", PAN shows "LAXMI SANTOSH GUPTA"
• **Date of birth** mismatch: PAN shows "2002-06-12", PAN shows "2004-12-20"
• PAN number mismatch: PAN shows "XXXXXX875K", PAN shows "XXXXXX189E"
• Father's name mismatch: PAN shows "AJIT SINGH", PAN shows "SANTOSH RAMASHARE GUPTA"
Other checks: Address not found on the documents
Please upload the correct documents:
- PAN
Still pending:
- Address Proof
- Bank Statement
- Signature

**Next step:** Upload these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [What should I do next?](ask:What%20should%20I%20do%20next?)
- [What can be used as address proof?](ask:What%20can%20be%20used%20as%20address%20proof?)

[Close](action:exit_case)

### D16. > documents for home loan  (0.24s)

Documents for Home Loan:

| Document | Required | Accepted as |
|---|---|---|
| PAN (Permanent Account Number) | Required | PAN |
| Bank Statement | Required | Bank Statement |
| Address Proof | Required | Driving Licence, Passport, Voter ID |
| Signature | Required | Signature |
| ITR (Income Tax Return) | Optional | ITR |
| Salary Slip | Optional | Salary Slip |
| Employment Proof | Optional | Employment Proof |

Every co-applicant:

| Document | Required | Accepted as |
|---|---|---|
| PAN | Required | PAN |
| Address Proof | Required | Aadhaar, Passport, Driving Licence, Voter ID, Utility Bill |
| Employment Proof | Required | Salary Slip, Employment Letter, Offer Letter, Employee ID, Business Registration, GST (Goods and Services Tax) Certificate, ITR |

Amount-based rules may add documents once the loan amount is entered.

[Close](action:exit_case)
