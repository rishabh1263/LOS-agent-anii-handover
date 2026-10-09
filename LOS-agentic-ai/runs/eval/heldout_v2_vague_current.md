# heldout_v2_vague (current)

```
{
 "set": "heldout_v2_vague",
 "label": "current",
 "total": 30,
 "correct": 19,
 "WRONG": 1,
 "not_available": 0,
 "clarify": 2,
 "case": 4,
 "other_miss": 4,
 "correct_pct": 63.3,
 "p50_ms": 110.3,
 "p95_ms": 3123.4,
 "model_pct": 0.0,
 "min_free_ram_gb": 3.11
}
```

| id | result | ms | model | question | reply |
|---|---|---|---|---|---|
| V001 | correct | 1523.9 |  | docs | CASE-2F2B314F0BDC About the documents, do you mean the list that is required, the ones still **pending**, the ones already submitted, or their verification status? You can ask: - [What documents are required?](ask:What%20documents%20are%20r |
| V002 | correct | 127.6 |  | kyc | Do you mean: [What is the KYC status?](ask:What%20is%20the%20KYC%20status?) · [Explain the process](ask:Explain%20the%20process) [Close](action:exit_case) |
| V003 | correct | 122.1 |  | status | Do you mean: [What is the KYC status?](ask:What%20is%20the%20KYC%20status?) · [What is the case status?](ask:What%20is%20the%20case%20status?) [Close](action:exit_case) |
| V004 | no_options | 841.8 |  | pending | CASE-2F2B314F0BDC Your KYC (Know Your Customer) verification needs attention: • **Name** mismatch: **PAN** (Permanent Account Number) shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA" Please uplo |
| V005 | correct | 3123.4 |  | co-applicant | Do you mean: [What is the KYC status?](ask:What%20is%20the%20KYC%20status?) · [What is pending?](ask:What%20is%20pending?) [Close](action:exit_case) |
| V006 | correct | 111.8 |  | pan | Do you mean: [What does the document say?](ask:What%20does%20the%20document%20say?) · [What is the KYC status?](ask:What%20is%20the%20KYC%20status?) [Close](action:exit_case) |
| V007 | correct | 153.2 |  | amount | CASE-2F2B314F0BDC Which amount do you mean: the loan amount, your declared income, your declared obligations, or the property value? 1. [What is my loan amount?](ask:What%20is%20my%20loan%20amount?) 2. [What income did I declare?](ask:What% |
| V008 | correct | 3133.3 |  | cpa | Do you mean: [Explain the process](ask:Explain%20the%20process) · [What is the case status?](ask:What%20is%20the%20case%20status?) [Close](action:exit_case) |
| V009 | correct | 107.8 |  | name | Do you mean: [What is the KYC status?](ask:What%20is%20the%20KYC%20status?) · [What does the document say?](ask:What%20does%20the%20document%20say?) [Close](action:exit_case) |
| V010 | no_options | 112.0 |  | upload | CASE-2F2B314F0BDC Your KYC (Know Your Customer) verification needs attention: • **Name** mismatch: **PAN** (Permanent Account Number) shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA" Please uplo |
| V011 | case | 93.7 |  | case | Which case is this about? Pick one and I'll answer "case" for it. You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**. Showing 1-2 of 2 (newest first). That is the whole list. |
| V012 | correct | 60.1 |  | top 2 | You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**. Showing 1-2 of 2 (newest first). That is the whole list. Do you want to know about a particular case? [Yes](ask:Yes) · [No](ask:No) Download: [Excel](a |
| V013 | case | 52.0 |  | cases | Which case is this about? Pick one and I'll answer "cases" for it. You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**. Showing 1-2 of 2 (newest first). That is the whole list. |
| V014 | no_options | 34.8 |  | help | You can ask me: - **My cases:** [Show my cases](ask:Show%20my%20cases) · [Which cases have a KYC issue?](ask:KYC%20wale%20cases) · [Which cases are ready for CPA?](ask:ready%20wale%20cases) · [Show my oldest cases](ask:sabse%20purane%20case |
| V015 | no_options | 3169.8 |  | query | CASE-2F2B314F0BDC I can help with your application details, its status and stage, your documents, and what to do next. Which of these did you mean? 1. [What are my application details?](ask:What%20are%20my%20application%20details?) 2. [What |
| V016 | correct | 135.8 |  | documents? | CASE-2F2B314F0BDC About the documents, do you mean the list that is required, the ones still **pending**, the ones already submitted, or their verification status? You can ask: - [What documents are required?](ask:What%20documents%20are%20r |
| V017 | correct | 105.5 |  | bank statement | CASE-2F2B314F0BDC - **Bank Statement** is **verified**. Its issuing authority has not been independently confirmed. You can ask: - [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?) - [What is the KYC status?](ask: |
| V018 | correct | 108.8 |  | next | CASE-2F2B314F0BDC **Next step:** Your next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue. You can ask: - [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for |
| V019 | correct | 113.3 |  | signature | CASE-2F2B314F0BDC - No Signature document has been uploaded for this case. You can ask: - [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?) - [What is the KYC status?](ask:What%20is%20the%20KYC%20status?) - [Why d |
| V020 | case | 55.9 |  | loan | Which case is this about? Pick one and I'll answer "loan" for it. You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**. Showing 1-2 of 2 (newest first). That is the whole list. |
| V021 | correct | 95.2 |  | docs -> 1 | CASE-2F2B314F0BDC - Sure: 3 required: PAN (Permanent Account Number) — VERIFIED; Address Proof — VERIFIED; Bank Statement — VERIFIED. 1 optional: Signature — MISSING. This list is not final: employment type has not been captured, so the rul |
| V022 | correct | 159.2 |  | kyc -> 1 | CASE-2F2B314F0BDC Not ready for CPA. KYC is not complete. - Applicant: name on the Bank Statement, differs from the application. - Applicant: name differs between the PAN and the Bank Statement. - Applicant: name differs between the Driving |
| V023 | WRONG | 75.0 |  | top 2 -> 1 | CASE-CE1EE0CBEF5B · Priya Verma · FOS PAN (Permanent Account Number), Address Proof, Bank Statement and Signature are not uploaded, so Priya Verma's KYC hasn't run yet. Next step: Upload the PAN. [Collect from customer](ask:Collect%20from%2 |
| V024 | correct | 95.1 |  | case -> 1 | CASE-CE1EE0CBEF5B - Your application has been created. PAN (Permanent Account Number), Address Proof, Bank Statement and 1 more are still **pending**. **Next step:** Want to see which documents are pending? You can ask: - [What is pending o |
| V025 | correct | 89.4 |  | status -> 2 | CASE-2F2B314F0BDC - Sure: your **PAN** (Permanent Account Number), driving licence and bank statement **passed** document verification. Its issuing authority has not been independently confirmed. You can ask: - [What is pending on this case |
| V026 | correct | 77.6 |  | pan -> 1 | CASE-2F2B314F0BDC - Here you go: your PAN number isn't available on the application yet. You can ask: - [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?) - [What is pending?](ask:What%20is%20pending?) - [Why is it |
| V027 | clarify | 153.9 |  | docs -> docs | CASE-2F2B314F0BDC Just to be sure, which one do you mean: 'What documents are required' or 'Which documents are **pending**' or 'Which documents have been submitted' or 'Are my documents **verified**'? Maybe one of these: [how do i create a |
| V028 | correct | 100.5 |  | status -> hmm status | CASE-2F2B314F0BDC - Here you go: your application has been created. Signature is still **pending**. **Next step:** Want to see which documents are pending? You can ask: - [What is pending on this case?](ask:What%20is%20pending%20on%20this%2 |
| V029 | clarify | 134.0 |  | kyc -> kyc? | Do you mean: [What is the KYC status?](ask:What%20is%20the%20KYC%20status?) · [Explain the process](ask:Explain%20the%20process) Maybe one of these: [what is kyc](ask:what%20is%20kyc) · [What does KYC mean?](ask:What%20does%20KYC%20mean?) · |
| V030 | case | 66.5 |  | case -> case | Which case is this about? Pick one and I'll answer "case" for it. You have 2 cases: 1 with a KYC issue, 1 with documents **pending**. Showing 1-2 of 2 (newest first). That is the whole list. |
