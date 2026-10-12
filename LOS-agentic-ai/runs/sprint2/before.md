# Sprint 2 live suite -- runs\sprint2\before

```
{
 "matrix": "19/25",
 "session": "20/27",
 "by_category": {
  "action:ambiguous": "1/1",
  "action:cancel": "3/3",
  "action:no": "2/2",
  "action:refuse": "1/1",
  "action:yes": "2/4",
  "isolation": "1/1",
  "ref:switch": "2/3",
  "short:clear": "5/7",
  "short:nocontext": "2/3"
 },
 "p50_ms": 199,
 "p95_ms": 4830,
 "max_ms": 10146,
 "model_calls_total": 16,
 "turns_with_model_call": 11
}
```

## Matrix

### PASS docs-ctx [short:clear] setup=['open CASE-3F175F32C726', 'what is pending?'] -> 'docs'
`356 ms, model_calls=0, meaning=pending_documents/embedding/0.99, audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

KYC needs attention:
• **Name** mismatch: **PAN** shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

### FAIL status-ctx [short:clear] setup=['open CASE-3F175F32C726'] -> 'status'
`196 ms, model_calls=0, meaning=None, audit=[]`  **problems: ['asked instead of answering', 'does not name c1']**

What would you like to know about status?

1. [What is the status of this case?](ask:What%20is%20the%20status%20of%20this%20case%3F)
2. [query status](ask:query%20status)
3. [What is the eligibility status?](ask:What%20is%20the%20eligibility%20status%3F)

[Close](action:exit_case)

### FAIL pending-ctx [short:clear] setup=['open CASE-3F175F32C726'] -> 'pending'
`168 ms, model_calls=0, meaning=None, audit=[]`  **problems: ['asked instead of answering', "lacks any of ('Address Proof', 'Signature')"]**

What would you like to know about **pending**?

1. [What is pending?](ask:What%20is%20pending%3F)
2. [Why is this case stuck?](ask:Why%20is%20this%20case%20stuck%3F)
3. [What should I do next?](ask:What%20should%20I%20do%20next%3F)

[Close](action:exit_case)

### PASS why-after-pending [short:clear] setup=['open CASE-3F175F32C726', 'what is pending?'] -> 'why'
`8624 ms, model_calls=2, meaning=None, audit=['CASE_HISTORY:OK:application.get']`

The application has recorded findings. The name on the PAN, RAHUL SHARMA, does not match the bank account holder name, ROHIT VERMA.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is pending?](ask:What%20is%20pending?)

[Close](action:exit_case)

### PASS kyun-after-kyc [short:clear] setup=['open CASE-3F175F32C726', 'kyc status?'] -> 'kyun'
`414 ms, model_calls=0, meaning=why_stuck/embedding/0.76, audit=['CASE_HISTORY:OK:application.get,workflow.pending_items,workflow.next_action']`

CASE-3F175F32C726 is stuck at FOS because:
- KYC failed: name do not match across the documents
- Not uploaded: Signature
- KYC not complete: date of birth, address, PAN number, father's name

**Next step:** Upload the Signature.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### PASS next-ctx [short:clear] setup=['open CASE-3F175F32C726', 'what is pending?'] -> 'next'
`2677 ms, model_calls=1, meaning=next_step/embedding/0.83, audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

Next step: Collect and upload the missing document: Signature. Wait for a reviewer to check the recorded issue.

[Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

### PASS aur-after-pan [short:clear] setup=['open CASE-3F175F32C726', 'is the PAN verified?'] -> 'aur bank statement?'
`428 ms, model_calls=0, meaning=None, audit=['DOCUMENT_VERIFICATION:OK:documents.verification,documents.checklist']`

**Bank Statement** is **verified**. Its issuing authority has not been independently confirmed.

- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

### PASS iska-after-switch [ref:switch] setup=['open CASE-3F175F32C726', 'open CASE-7F932D6F5C64'] -> 'iska pending kya hai?'
`356 ms, model_calls=0, meaning=pending_documents/embedding/0.90, audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-7F932D6F5C64
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

### FAIL pehle-wale [ref:switch] setup=['open CASE-3F175F32C726', 'open CASE-7F932D6F5C64', 'pehle wale par wapas chalo'] -> 'status'
`117 ms, model_calls=0, meaning=None, audit=[]`  **problems: ['does not name c1']**

What would you like to know about status?

1. [What is the status of this case?](ask:What%20is%20the%20status%20of%20this%20case%3F)
2. [query status](ask:query%20status)
3. [What is the eligibility status?](ask:What%20is%20the%20eligibility%20status%3F)

[Close](action:exit_case)

### PASS woh-wala [ref:switch] setup=['open CASE-3F175F32C726', 'open CASE-7F932D6F5C64'] -> 'woh pehle wala case kholo'
`78 ms, model_calls=0, meaning=None, audit=[]`

CASE-3F175F32C726 · Rahul Sharma · FOS
Applicant: name **did not pass**: the documents disagree.
Next step: Upload the Signature.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

[Close](action:exit_case)

### PASS docs-nocase [short:nocontext] setup=[] -> 'docs'
`68 ms, model_calls=0, meaning=None, audit=[]`

What would you like to know about docs?

1. [Show cases with documents pending](ask:Show%20cases%20with%20documents%20pending)
2. [Which documents are needed for a loan?](ask:Which%20documents%20are%20needed%20for%20a%20loan%3F)
3. [How do I upload documents?](ask:How%20do%20I%20upload%20documents%3F)

### FAIL why-nocase [short:nocontext] setup=[] -> 'why'
`1796 ms, model_calls=1, meaning=why_stuck/model/0.66, audit=[]`  **problems: ["says 'Rahul'"]**

Which case is this about? Pick one and I'll answer "Why is this case stuck?" for it.

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-3F175F32C726 | Rahul S. (APP-0A5A7EA193EA) | FOS (Field Officer Sales) | Created · KYC issue | [Open](action:open_case?id=CASE-3F175F32C726) |
| 2 | CASE-7F932D6F5C64 | Priya V. (APP-8DAFCC3B0CA7) | FOS | Created · 5 docs pending | [Open](action:open_case?id=CASE-7F932D6F5C64) |

Showing 1-2 of 2 (newest first).
That is the whole list.

### PASS yes-nothing-pending [action:yes] setup=[] -> 'yes'
`31 ms, model_calls=0, meaning=None, audit=[]`

Which case is this about? Pick one and I'll answer "yes" for it.

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-3F175F32C726 | Rahul S. (APP-0A5A7EA193EA) | FOS (Field Officer Sales) | Created · KYC issue | [Open](action:open_case?id=CASE-3F175F32C726) |
| 2 | CASE-7F932D6F5C64 | Priya V. (APP-8DAFCC3B0CA7) | FOS | Created · 5 docs pending | [Open](action:open_case?id=CASE-7F932D6F5C64) |

Showing 1-2 of 2 (newest first).
That is the whole list.

### PASS cancel-nothing [action:cancel] setup=[] -> 'cancel'
`161 ms, model_calls=0, meaning=command/embedding/1.00, audit=[]`

Which case is this about? Pick one and I'll answer "cancel" for it.

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-3F175F32C726 | Rahul S. (APP-0A5A7EA193EA) | FOS (Field Officer Sales) | Created · KYC issue | [Open](action:open_case?id=CASE-3F175F32C726) |
| 2 | CASE-7F932D6F5C64 | Priya V. (APP-8DAFCC3B0CA7) | FOS | Created · 5 docs pending | [Open](action:open_case?id=CASE-7F932D6F5C64) |

Showing 1-2 of 2 (newest first).
That is the whole list.

### PASS iska-nocase [short:nocontext] setup=[] -> 'iska status?'
`156 ms, model_calls=0, meaning=case_status/embedding/0.81, audit=[]`

Which case is this about? Pick one and I'll answer "What is the status of this case?" for it.

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-3F175F32C726 | Rahul S. (APP-0A5A7EA193EA) | FOS (Field Officer Sales) | Created · KYC issue | [Open](action:open_case?id=CASE-3F175F32C726) |
| 2 | CASE-7F932D6F5C64 | Priya V. (APP-8DAFCC3B0CA7) | FOS | Created · 5 docs pending | [Open](action:open_case?id=CASE-7F932D6F5C64) |

Showing 1-2 of 2 (newest first).
That is the whole list.

### FAIL yes-confirms [action:yes] setup=['open CASE-3F175F32C726', 'loan amount 6 lakh karo'] -> 'yes'
`199 ms, model_calls=0, meaning=command/embedding/1.00, audit=['ACKNOWLEDGEMENT:CONVERSATION:']`  **problems: ['store loan_amount=500000, expected 600000']**

CASE-3F175F32C726
Sure Let me know if you'd like to check anything else.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### FAIL haan-confirms [action:yes] setup=['open CASE-3F175F32C726', 'loan amount 6 lakh karo'] -> 'haan'
`1678 ms, model_calls=1, meaning=None, audit=['ACKNOWLEDGEMENT:CONVERSATION:']`  **problems: ['store loan_amount=500000, expected 600000']**

CASE-3F175F32C726
Sure Let me know if you'd like to check anything else.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### PASS no-declines [action:no] setup=['open CASE-3F175F32C726', 'loan amount 7 lakh karo'] -> 'no'
`280 ms, model_calls=0, meaning=command/embedding/1.00, audit=[]`

CASE-3F175F32C726
Okay. Want to see what is **pending** instead?

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### PASS nahi-declines [action:no] setup=['open CASE-3F175F32C726', 'loan amount 7 lakh karo'] -> 'nahi'
`229 ms, model_calls=0, meaning=None, audit=[]`

CASE-3F175F32C726
Okay. Want to see what is **pending** instead?

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### PASS cancel-pending [action:cancel] setup=['open CASE-3F175F32C726', 'loan amount 7 lakh karo'] -> 'cancel'
`46 ms, model_calls=0, meaning=command/embedding/1.00, audit=[]`

Okay, nothing was changed.

[Close](action:exit_case)

### PASS yes-after-cancel [action:cancel] setup=['open CASE-3F175F32C726', 'loan amount 7 lakh karo', 'cancel'] -> 'yes'
`48 ms, model_calls=0, meaning=None, audit=['ACKNOWLEDGEMENT:CONVERSATION:']`

CASE-3F175F32C726
Sure Let me know if you'd like to check anything else.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### PASS yes-not-blanket [action:yes] setup=['open CASE-3F175F32C726', 'what is pending?'] -> 'yes'
`41 ms, model_calls=0, meaning=None, audit=['ACKNOWLEDGEMENT:CONVERSATION:']`

Sure Let me know if you'd like to check anything else.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### PASS kar-do-ambiguous [action:ambiguous] setup=['open CASE-3F175F32C726'] -> 'kar do'
`214 ms, model_calls=0, meaning=ambiguous_action/embedding/1.00, audit=[]`

What should I do? Pick one:

1. [Collect from customer](ask:Collect%20from%20customer)
2. [What is pending?](ask:What%20is%20pending%3F)
3. [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA%3F)
4. [Move to CPA](ask:Move%20to%20CPA)

[Close](action:exit_case)

### PASS move-not-ready [action:refuse] setup=['open CASE-3F175F32C726'] -> 'CPA pe move kar do'
`58 ms, model_calls=0, meaning=None, audit=[]`

CASE-3F175F32C726 can't move to CPA yet, these need to be fixed first:
Applicant documents
- Signature: PENDING, not uploaded yet. Fix: Upload the Signature.
Passed: PAN (Permanent Account Number), Address Proof, Bank Statement.

Signature
- Signature: PENDING. Signature has not been uploaded. Fix: Upload the signature.

KYC (Know Your Customer)
- Applicant: name: FAILED, the documents disagree. Fix: Upload a correct document so the name matches.
- Applicant: date of birth: PENDING, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.
- Applicant: address: PENDING, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.
- Applicant: PAN number: PENDING, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.
- Applicant: father's name: PENDING, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.

**Next step:** Upload the Signature.

[Collect from customer](ask:Collect%20from%20customer)

[Close](action:exit_case)

### PASS named-other [isolation] setup=['open CASE-3F175F32C726'] -> 'Priya ka pending kya hai?'
`69 ms, model_calls=0, meaning=None, audit=['DOCUMENTS_PENDING:OK:documents.get,workflow.pending_items,documents.checklist']`

CASE-7F932D6F5C64
Still pending:
- **PAN** (Permanent Account Number)
- **Address Proof**
- Bank Statement
- Signature

**Next step:** Upload these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What should I do next?](ask:What%20should%20I%20do%20next?)

[Close](action:exit_case)

## Continuous session

### 1. PASS > Good morning! Aaj kis case par pehle kaam karun?
`28 ms, model_calls=0, meaning=None, audit=[]`

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-3F175F32C726 | Rahul S. (APP-0A5A7EA193EA) | FOS (Field Officer Sales) | Created · KYC issue | [Open](action:open_case?id=CASE-3F175F32C726) |

Showing 1-1 of 2 (newest first).

[Show more](action:list_more)

Do you want to know about a particular case?
[Yes](ask:Yes) · [No](ask:No)

Download: [Excel](action:download_list?format=xlsx&q=all) · [Doc](action:download_list?format=docx&q=all) · [PDF](action:download_list?format=pdf&q=all) · [Show in UI](action:show_list_in_ui?q=all)

### 2. PASS > Rahul Sharma ka case kholo
`1590 ms, model_calls=1, meaning=None, audit=[]`

CASE-3F175F32C726 · Rahul Sharma · FOS
Applicant: name **did not pass**: the documents disagree.
Next step: Upload the Signature.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

[Close](action:exit_case)

### 3. PASS > status batao
`4830 ms, model_calls=1, meaning=case_status/embedding/1.00, audit=['FULL_SUMMARY:OK:applicant.360']`

CASE-3F175F32C726
- Rahul Sharma — the application is at Basic Document Verification. Completed: **PAN** (Permanent Account Number), Driving Licence, Bank Statement. **Pending**: Signature has not been uploaded. Signature has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 2 item(s) blocking.

Download: [Excel](action:download?format=xlsx&case=CASE-3F175F32C726) · [Doc](action:download?format=docx&case=CASE-3F175F32C726) · [PDF](action:download?format=pdf&case=CASE-3F175F32C726) · [Show in UI](action:show_in_ui?case=CASE-3F175F32C726)

- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)

[Close](action:exit_case)

### 4. PASS > pending
`184 ms, model_calls=0, meaning=None, audit=[]`

What would you like to know about **pending**?

1. [What is pending?](ask:What%20is%20pending%3F)
2. [Why is this case stuck?](ask:Why%20is%20this%20case%20stuck%3F)
3. [What should I do next?](ask:What%20should%20I%20do%20next%3F)

[Close](action:exit_case)

### 5. PASS > why
`2778 ms, model_calls=1, meaning=why_stuck/model/0.66, audit=['CASE_HISTORY:OK:application.get,workflow.pending_items,workflow.next_action']`

CASE-3F175F32C726 is stuck at FOS because:
- KYC failed: name do not match across the documents
- Not uploaded: Signature
- KYC not complete: date of birth, address, PAN number, father's name

**Next step:** Upload the Signature.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is pending?](ask:What%20is%20pending?)

[Close](action:exit_case)

### 6. FAIL > KYC mein kya issue hai?
`208 ms, model_calls=0, meaning=why_stuck/embedding/0.93, audit=['CASE_HISTORY:OK:application.get,workflow.pending_items,workflow.next_action']`  **problems: ["lacks any of ('ROHIT VERMA',)"]**

CASE-3F175F32C726 is stuck at FOS because:
- KYC failed: name do not match across the documents
- Not uploaded: Signature
- KYC not complete: date of birth, address, PAN number, father's name

**Next step:** Upload the Signature.

- [Why is it stuck?](ask:Why%20is%20it%20stuck?)
- [Co-applicant](ask:Co-applicant)

[Close](action:exit_case)

### 7. FAIL > docs
`119 ms, model_calls=0, meaning=None, audit=[]`  **problems: ['asked instead of answering']**

What would you like to know about docs?

1. [What is pending?](ask:What%20is%20pending%3F)
2. [Why is this case stuck?](ask:Why%20is%20this%20case%20stuck%3F)
3. [What should I do next?](ask:What%20should%20I%20do%20next%3F)

[Close](action:exit_case)

### 8. PASS > is signature mandatory?
`148 ms, model_calls=0, meaning=knowledge_documents/embedding/0.99, audit=[]`

Yes, Signature is mandatory for every loan product.

[Close](action:exit_case)

### 9. PASS > next
`2311 ms, model_calls=1, meaning=next_step/embedding/0.83, audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

Next step: Collect and upload the missing document: Signature. Wait for a reviewer to check the recorded issue.

[Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### 10. PASS > Ek minute, Priya Verma ka case check karo
`85 ms, model_calls=0, meaning=None, audit=['DOCUMENT_VERIFICATION:OK:documents.get,documents.checklist']`

CASE-7F932D6F5C64
- No documents currently have verification issues.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [Why is PAN required for this application?](ask:Why%20is%20PAN%20required%20for%20this%20application?)

[Close](action:exit_case)

### 11. FAIL > iska pending kya hai?
`165 ms, model_calls=0, meaning=pending_documents/embedding/0.90, audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`  **problems: ['does not name c2']**

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

### 12. PASS > Ab pehle wale par wapas chalo
`48 ms, model_calls=0, meaning=None, audit=[]`

CASE-3F175F32C726 · Rahul Sharma · FOS
Applicant: name **did not pass**: the documents disagree.
Next step: Upload the Signature.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

[Close](action:exit_case)

### 13. PASS > uska status change hua?
`6140 ms, model_calls=2, meaning=case_status/model/0.67, audit=['FULL_SUMMARY:OK:applicant.360']`

CASE-3F175F32C726
- Rahul Sharma — the application is at Basic Document Verification. Completed: **PAN** (Permanent Account Number), Driving Licence, Bank Statement. **Pending**: Signature has not been uploaded. Signature has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 2 item(s) blocking.

Download: [Excel](action:download?format=xlsx&case=CASE-3F175F32C726) · [Doc](action:download?format=docx&case=CASE-3F175F32C726) · [PDF](action:download?format=pdf&case=CASE-3F175F32C726) · [Show in UI](action:show_in_ui?case=CASE-3F175F32C726)

- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)

[Close](action:exit_case)

### 14. PASS > loan amount 6 lakh karo
`45 ms, model_calls=0, meaning=None, audit=[]`

Change Loan amount (Rs) of CASE-3F175F32C726 from 500000 to 600000?

[Confirm](action:confirm_write?ref=edit-3e82e5) · [Cancel](action:cancel_write?ref=edit-3e82e5)

[Close](action:exit_case)

### 15. FAIL > haan
`1673 ms, model_calls=1, meaning=None, audit=['ACKNOWLEDGEMENT:CONVERSATION:']`  **problems: ['store loan_amount=500000, expected 600000']**

Sure Let me know if you'd like to check anything else.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### 16. FAIL > loan amount kitna hai ab?
`351 ms, model_calls=0, meaning=loan_amount/embedding/0.79, audit=['APPLICANT_PROFILE:OK:application.get']`  **problems: ["lacks any of ('6,00,000', '600000')"]**

You applied for **₹5,00,000**.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### 17. PASS > loan amount 7 lakh karo
`23 ms, model_calls=0, meaning=None, audit=[]`

Change Loan amount (Rs) of CASE-3F175F32C726 from 500000 to 700000?

[Confirm](action:confirm_write?ref=edit-6ac4de) · [Cancel](action:cancel_write?ref=edit-6ac4de)

[Close](action:exit_case)

### 18. FAIL > ruko, cancel karo
`10146 ms, model_calls=4, meaning=None, audit=['UNKNOWN:UNSUPPORTED:']`  **problems: ['store loan_amount=500000, expected 600000']**

That's outside what I can help with here. I'm set up for the loan application. I can check its status, documents, verification or KYC (Know Your Customer) for you.

- [What are my application details?](ask:What%20are%20my%20application%20details?)
- [What is my application status?](ask:What%20is%20my%20application%20status?)

[Close](action:exit_case)

### 19. FAIL > yes
`257 ms, model_calls=0, meaning=None, audit=[]`  **problems: ['store loan_amount=500000, expected 600000']**

A yes does not tell me which one, do you mean 'What are my application details' or 'What is my application status' or 'What documents are still needed' or 'Which documents have been **verified**' or 'What is **pending** on this case'?

Maybe one of these:
[Why is it stuck?](ask:Why%20is%20it%20stuck?) · [What does KYC mean?](ask:What%20does%20KYC%20mean?) · [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)
Talk to my supervisor: contact your branch supervisor or the FOS helpdesk.

[Close](action:exit_case)

### 20. PASS > nahi mera matlab bank statement ka status
`300 ms, model_calls=0, meaning=None, audit=['DOCUMENT_VERIFICATION:OK:documents.verification,documents.checklist']`

Right: **Bank Statement** is **verified**. Its issuing authority has not been independently confirmed.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)

[Close](action:exit_case)

### 21. PASS > Is this case ready for CPA?
`189 ms, model_calls=0, meaning=None, audit=['READINESS:OK:workflow.readiness,documents.checklist']`

CPA readiness: 4 of 11 checks passed.

**Applicant documents**
- Signature: **PENDING**, not uploaded yet. Fix: Upload the Signature.
Passed: PAN, Address Proof, Bank Statement.

**Signature**
- Signature: **PENDING**. Signature has not been uploaded. Fix: Upload the signature.

**KYC**
- Applicant: name: **FAILED**, the documents disagree. Fix: Upload a correct document so the name matches.
- Applicant: date of birth: **PENDING**, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.
- Applicant: address: **PENDING**, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.
- Applicant: PAN number: **PENDING**, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.
- Applicant: father's name: **PENDING**, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.

**Next step:** Upload the Signature.

- [What should I do next?](ask:What%20should%20I%20do%20next?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### 22. PASS > CPA pe move kar do
`115 ms, model_calls=0, meaning=None, audit=[]`

CASE-3F175F32C726 can't move to CPA yet, these need to be fixed first:
Applicant documents
- Signature: PENDING, not uploaded yet. Fix: Upload the Signature.
Passed: PAN (Permanent Account Number), Address Proof, Bank Statement.

Signature
- Signature: PENDING. Signature has not been uploaded. Fix: Upload the signature.

KYC (Know Your Customer)
- Applicant: name: FAILED, the documents disagree. Fix: Upload a correct document so the name matches.
- Applicant: date of birth: PENDING, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.
- Applicant: address: PENDING, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.
- Applicant: PAN number: PENDING, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.
- Applicant: father's name: PENDING, no KYC result recorded for this check. Fix: Upload the identity documents so KYC can run.

**Next step:** Upload the Signature.

[Collect from customer](ask:Collect%20from%20customer)

[Close](action:exit_case)

### 23. PASS > what documents are needed for personal loan?
`352 ms, model_calls=0, meaning=None, audit=['FOS_KNOWLEDGE:OK:knowledge.fos']`

Required: PAN, Address Proof (Driving Licence, Passport or Voter ID), Bank Statement, Signature.

**Next step:** Check your status or **pending** documents?

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### 24. PASS > can I upload aadhaar instead of pan?
`263 ms, model_calls=0, meaning=knowledge_documents/embedding/1.00, audit=[]`

No. Aadhaar cannot replace it: the PAN (Permanent Account Number) requirement accepts only PAN.

[Close](action:exit_case)

### 25. PASS > what is api
`37 ms, model_calls=0, meaning=None, audit=['GUARDRAIL_BLOCKED:BLOCKED:']`

I can't share details of the systems behind this assistant. I can help with your cases, documents, KYC (Know Your Customer) and the steps to move a case to CPA.

### 26. PASS > thanks
`370 ms, model_calls=0, meaning=small_talk/embedding/1.00, audit=['THANKS:CONVERSATION:']`

Anytime Let me know if you need anything else.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### 27. PASS > Abhi tak kya kya pending hai, ek summary do
`155 ms, model_calls=0, meaning=None, audit=['FULL_SUMMARY:OK:applicant.360']`

Rahul Sharma — the application is at Basic Document Verification. Completed: **PAN**, Driving Licence, Bank Statement. **Pending**: Signature has not been uploaded. Signature has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 2 item(s) blocking.

[Collect from customer](ask:Collect%20from%20customer) · Download: [Excel](action:download?format=xlsx&case=CASE-3F175F32C726) · [Doc](action:download?format=docx&case=CASE-3F175F32C726) · [PDF](action:download?format=pdf&case=CASE-3F175F32C726) · [Show in UI](action:show_in_ui?case=CASE-3F175F32C726)

[Close](action:exit_case)
