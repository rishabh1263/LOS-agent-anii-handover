# Sprint 2 live suite -- runs\sprint2\final

```
{
 "matrix": "25/25",
 "session": "27/27",
 "by_category": {
  "action:ambiguous": "1/1",
  "action:cancel": "3/3",
  "action:no": "2/2",
  "action:refuse": "1/1",
  "action:yes": "4/4",
  "isolation": "1/1",
  "ref:switch": "3/3",
  "short:clear": "7/7",
  "short:nocontext": "3/3"
 },
 "p50_ms": 412,
 "p95_ms": 5484,
 "max_ms": 9265,
 "model_calls_total": 15,
 "turns_with_model_call": 13
}
```

## Matrix

### PASS docs-ctx [short:clear] setup=['open CASE-28006A0D7DEE', 'what is pending?'] -> 'docs'
`699 ms, model_calls=0, meaning=pending_documents/embedding/0.99, audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

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

### PASS status-ctx [short:clear] setup=['open CASE-28006A0D7DEE'] -> 'status'
`5509 ms, model_calls=1, meaning=case_status/embedding/0.99, audit=['FULL_SUMMARY:OK:applicant.360']`

CASE-28006A0D7DEE
- Rahul Sharma — the application is at Basic Document Verification. Completed: **PAN** (Permanent Account Number), Driving Licence, Bank Statement. **Pending**: Signature has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 2 item(s) blocking.

Download: [Excel](action:download?format=xlsx&case=CASE-28006A0D7DEE) · [Doc](action:download?format=docx&case=CASE-28006A0D7DEE) · [PDF](action:download?format=pdf&case=CASE-28006A0D7DEE) · [Show in UI](action:show_in_ui?case=CASE-28006A0D7DEE)

- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)

[Close](action:exit_case)

### PASS pending-ctx [short:clear] setup=['open CASE-28006A0D7DEE'] -> 'pending'
`517 ms, model_calls=0, meaning=pending_documents/embedding/0.99, audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-28006A0D7DEE
KYC (Know Your Customer) needs attention:
• **Name** mismatch: **PAN** (Permanent Account Number) shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### PASS why-after-pending [short:clear] setup=['open CASE-28006A0D7DEE', 'what is pending?'] -> 'why'
`9265 ms, model_calls=2, meaning=None, audit=['CASE_HISTORY:OK:application.get']`

The application has recorded findings. The name on the PAN, RAHUL SHARMA, does not match the bank account holder name, ROHIT VERMA.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is pending?](ask:What%20is%20pending?)

[Close](action:exit_case)

### PASS kyun-after-kyc [short:clear] setup=['open CASE-28006A0D7DEE', 'kyc status?'] -> 'kyun'
`494 ms, model_calls=0, meaning=why_stuck/embedding/0.76, audit=['CASE_HISTORY:OK:application.get,workflow.pending_items,workflow.next_action']`

CASE-28006A0D7DEE is stuck at FOS because:
- KYC failed: mismatch in name across the documents
- Not uploaded: Signature
- KYC not complete: date of birth, address, PAN number, father's name

**Next step:** Upload the Signature.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### PASS next-ctx [short:clear] setup=['open CASE-28006A0D7DEE', 'what is pending?'] -> 'next'
`5412 ms, model_calls=1, meaning=next_step/embedding/0.83, audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

**Next step:** The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

[Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

### PASS aur-after-pan [short:clear] setup=['open CASE-28006A0D7DEE', 'is the PAN verified?'] -> 'aur bank statement?'
`335 ms, model_calls=0, meaning=None, audit=['DOCUMENT_VERIFICATION:OK:documents.verification,documents.checklist']`

**Bank Statement** is **verified**. Its issuing authority has not been independently confirmed.

- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

### PASS iska-after-switch [ref:switch] setup=['open CASE-28006A0D7DEE', 'open CASE-69469C6809C3'] -> 'iska pending kya hai?'
`446 ms, model_calls=0, meaning=pending_documents/embedding/0.90, audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-69469C6809C3
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

### PASS pehle-wale [ref:switch] setup=['open CASE-28006A0D7DEE', 'open CASE-69469C6809C3', 'pehle wale par wapas chalo'] -> 'status'
`5335 ms, model_calls=1, meaning=case_status/embedding/0.99, audit=['FULL_SUMMARY:OK:applicant.360']`

CASE-28006A0D7DEE
- Rahul Sharma — the application is at Basic Document Verification. Completed: **PAN** (Permanent Account Number), Driving Licence, Bank Statement. **Pending**: Signature has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 2 item(s) blocking.

Download: [Excel](action:download?format=xlsx&case=CASE-28006A0D7DEE) · [Doc](action:download?format=docx&case=CASE-28006A0D7DEE) · [PDF](action:download?format=pdf&case=CASE-28006A0D7DEE) · [Show in UI](action:show_in_ui?case=CASE-28006A0D7DEE)

- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)

[Close](action:exit_case)

### PASS woh-wala [ref:switch] setup=['open CASE-28006A0D7DEE', 'open CASE-69469C6809C3'] -> 'woh pehle wala case kholo'
`134 ms, model_calls=0, meaning=None, audit=[]`

CASE-28006A0D7DEE · Rahul Sharma · FOS
Applicant: name **did not pass**: the documents disagree.
Next step: Upload the Signature.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

[Close](action:exit_case)

### PASS docs-nocase [short:nocontext] setup=[] -> 'docs'
`147 ms, model_calls=0, meaning=None, audit=[]`

What would you like to know about docs?

1. [Show cases with documents pending](ask:Show%20cases%20with%20documents%20pending)
2. [Which documents are needed for a loan?](ask:Which%20documents%20are%20needed%20for%20a%20loan%3F)
3. [How do I upload documents?](ask:How%20do%20I%20upload%20documents%3F)

### PASS why-nocase [short:nocontext] setup=[] -> 'why'
`4274 ms, model_calls=1, meaning=None, audit=[]`

Which case is this about? Pick one and I'll answer "why" for it.

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-28006A0D7DEE | Rahul S. (APP-B1CC04505488) | FOS (Field Officer Sales) | Created · KYC issue | [Open](action:open_case?id=CASE-28006A0D7DEE) |
| 2 | CASE-69469C6809C3 | Priya V. (APP-F2C7D0154807) | FOS | Created · 5 docs pending | [Open](action:open_case?id=CASE-69469C6809C3) |

Showing 1-2 of 2 (newest first).
That is the whole list.

### PASS yes-nothing-pending [action:yes] setup=[] -> 'yes'
`87 ms, model_calls=0, meaning=None, audit=[]`

Which case is this about? Pick one and I'll answer "yes" for it.

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-28006A0D7DEE | Rahul S. (APP-B1CC04505488) | FOS (Field Officer Sales) | Created · KYC issue | [Open](action:open_case?id=CASE-28006A0D7DEE) |
| 2 | CASE-69469C6809C3 | Priya V. (APP-F2C7D0154807) | FOS | Created · 5 docs pending | [Open](action:open_case?id=CASE-69469C6809C3) |

Showing 1-2 of 2 (newest first).
That is the whole list.

### PASS cancel-nothing [action:cancel] setup=[] -> 'cancel'
`297 ms, model_calls=0, meaning=command/embedding/1.00, audit=[]`

Which case is this about? Pick one and I'll answer "cancel" for it.

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-28006A0D7DEE | Rahul S. (APP-B1CC04505488) | FOS (Field Officer Sales) | Created · KYC issue | [Open](action:open_case?id=CASE-28006A0D7DEE) |
| 2 | CASE-69469C6809C3 | Priya V. (APP-F2C7D0154807) | FOS | Created · 5 docs pending | [Open](action:open_case?id=CASE-69469C6809C3) |

Showing 1-2 of 2 (newest first).
That is the whole list.

### PASS iska-nocase [short:nocontext] setup=[] -> 'iska status?'
`412 ms, model_calls=0, meaning=case_status/embedding/0.81, audit=[]`

Which case is this about? Pick one and I'll answer "What is the status of this case?" for it.

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-28006A0D7DEE | Rahul S. (APP-B1CC04505488) | FOS (Field Officer Sales) | Created · KYC issue | [Open](action:open_case?id=CASE-28006A0D7DEE) |
| 2 | CASE-69469C6809C3 | Priya V. (APP-F2C7D0154807) | FOS | Created · 5 docs pending | [Open](action:open_case?id=CASE-69469C6809C3) |

Showing 1-2 of 2 (newest first).
That is the whole list.

### PASS yes-confirms [action:yes] setup=['open CASE-28006A0D7DEE', 'loan amount 6 lakh karo'] -> 'yes'
`309 ms, model_calls=0, meaning=command/embedding/1.00, audit=['UPDATE_CASE_FORM:OK:application.update']`

Updated Loan amount (Rs) of CASE-28006A0D7DEE to 600000.

[Close](action:exit_case)

### PASS haan-confirms [action:yes] setup=['open CASE-28006A0D7DEE', 'loan amount 6 lakh karo'] -> 'haan'
`4273 ms, model_calls=1, meaning=None, audit=['UPDATE_CASE_FORM:OK:application.update']`

Updated Loan amount (Rs) of CASE-28006A0D7DEE to 600000.

[Close](action:exit_case)

### PASS no-declines [action:no] setup=['open CASE-28006A0D7DEE', 'loan amount 7 lakh karo'] -> 'no'
`324 ms, model_calls=0, meaning=command/embedding/1.00, audit=[]`

Okay, nothing was changed.

[Close](action:exit_case)

### PASS nahi-declines [action:no] setup=['open CASE-28006A0D7DEE', 'loan amount 7 lakh karo'] -> 'nahi'
`271 ms, model_calls=0, meaning=None, audit=[]`

Okay, nothing was changed.

[Close](action:exit_case)

### PASS cancel-pending [action:cancel] setup=['open CASE-28006A0D7DEE', 'loan amount 7 lakh karo'] -> 'cancel'
`179 ms, model_calls=0, meaning=command/embedding/1.00, audit=[]`

Okay, nothing was changed.

[Close](action:exit_case)

### PASS yes-after-cancel [action:cancel] setup=['open CASE-28006A0D7DEE', 'loan amount 7 lakh karo', 'cancel'] -> 'yes'
`1147 ms, model_calls=0, meaning=None, audit=['ACKNOWLEDGEMENT:CONVERSATION:']`

CASE-28006A0D7DEE
Sure. Let me know if you'd like to check anything else.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### PASS yes-not-blanket [action:yes] setup=['open CASE-28006A0D7DEE', 'what is pending?'] -> 'yes'
`129 ms, model_calls=0, meaning=None, audit=['ACKNOWLEDGEMENT:CONVERSATION:']`

Sure. Let me know if you'd like to check anything else.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### PASS kar-do-ambiguous [action:ambiguous] setup=['open CASE-28006A0D7DEE'] -> 'kar do'
`278 ms, model_calls=0, meaning=ambiguous_action/embedding/1.00, audit=[]`

What should I do? Pick one:

1. [Collect from customer](ask:Collect%20from%20customer)
2. [What is pending?](ask:What%20is%20pending%3F)
3. [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA%3F)
4. [Move to CPA](ask:Move%20to%20CPA)

[Close](action:exit_case)

### PASS move-not-ready [action:refuse] setup=['open CASE-28006A0D7DEE'] -> 'CPA pe move kar do'
`228 ms, model_calls=0, meaning=None, audit=[]`

CASE-28006A0D7DEE can't move to CPA yet, these need to be fixed first:
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

### PASS named-other [isolation] setup=['open CASE-28006A0D7DEE'] -> 'Priya ka pending kya hai?'
`260 ms, model_calls=0, meaning=None, audit=['DOCUMENTS_PENDING:OK:documents.get,workflow.pending_items,documents.checklist']`

CASE-69469C6809C3
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
`116 ms, model_calls=0, meaning=None, audit=[]`

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-28006A0D7DEE | Rahul S. (APP-B1CC04505488) | FOS (Field Officer Sales) | Created · KYC issue | [Open](action:open_case?id=CASE-28006A0D7DEE) |

Showing 1-1 of 2 (newest first).

[Show more](action:list_more)

Do you want to know about a particular case?
[Yes](ask:Yes) · [No](ask:No)

Download: [Excel](action:download_list?format=xlsx&q=all) · [Doc](action:download_list?format=docx&q=all) · [PDF](action:download_list?format=pdf&q=all) · [Show in UI](action:show_list_in_ui?q=all)

### 2. PASS > Rahul Sharma ka case kholo
`2151 ms, model_calls=1, meaning=None, audit=[]`

CASE-28006A0D7DEE · Rahul Sharma · FOS
Applicant: name **did not pass**: the documents disagree.
Next step: Upload the Signature.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

[Close](action:exit_case)

### 3. PASS > status batao
`5421 ms, model_calls=1, meaning=case_status/embedding/1.00, audit=['FULL_SUMMARY:OK:applicant.360']`

CASE-28006A0D7DEE
- Rahul Sharma — the application is at Basic Document Verification. Completed: **PAN** (Permanent Account Number), Driving Licence, Bank Statement. **Pending**: Signature has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 2 item(s) blocking.

Download: [Excel](action:download?format=xlsx&case=CASE-28006A0D7DEE) · [Doc](action:download?format=docx&case=CASE-28006A0D7DEE) · [PDF](action:download?format=pdf&case=CASE-28006A0D7DEE) · [Show in UI](action:show_in_ui?case=CASE-28006A0D7DEE)

- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)

[Close](action:exit_case)

### 4. PASS > pending
`543 ms, model_calls=0, meaning=pending_documents/embedding/0.99, audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

KYC (Know Your Customer) needs attention:
• **Name** mismatch: **PAN** shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### 5. PASS > why
`9235 ms, model_calls=2, meaning=None, audit=['CASE_HISTORY:OK:application.get']`

The application has recorded findings. The name on the PAN, RAHUL SHARMA, does not match the bank account holder name, ROHIT VERMA.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is pending?](ask:What%20is%20pending?)

[Close](action:exit_case)

### 6. PASS > KYC mein kya issue hai?
`522 ms, model_calls=0, meaning=kyc_status/embedding/0.99, audit=['KYC_RESULT:OK:']`

Not ready for CPA. KYC is not complete.
- Applicant: name on the Bank Statement, differs from the application.
- Applicant: name differs between the PAN and the Bank Statement.
- Applicant: name differs between the Driving Licence and the Bank Statement.

**Next step:** Ask the customer to upload correct documents that match the application form (Applicant: Bank Statement).
[Draft a message to the customer](ask:Draft%20a%20message%20to%20the%20customer)

**KYC, Applicant**

*A. Application form vs documents*

| Field | Form value | Document | Document value | Result |
|---|---|---|---|---|
| Name | Rahul Sharma | PAN | RAHUL SHARMA | Match |
| Name | Rahul Sharma | Driving Licence | RAHUL SHARMA | Match |
| Name | Rahul Sharma | Bank Statement | ROHIT VERMA | Differs from the application |

*B. Documents vs each other*

| Field | Document 1 | Value | Document 2 | Value | Result |
|---|---|---|---|---|---|
| Name | PAN | RAHUL SHARMA | Driving Licence | RAHUL SHARMA | Match |
| Name | PAN | RAHUL SHARMA | Bank Statement | ROHIT VERMA | Differs |
| Name | Driving Licence | RAHUL SHARMA | Bank Statement | ROHIT VERMA | Differs |

Likely odd one out: the Bank Statement (name), the other documents agree. Upload a correct Bank Statement.

- [Which documents does KYC compare?](ask:Which%20documents%20does%20KYC%20compare?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### 7. PASS > docs
`555 ms, model_calls=0, meaning=pending_documents/embedding/0.99, audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

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

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### 8. PASS > is signature mandatory?
`303 ms, model_calls=0, meaning=knowledge_documents/embedding/0.99, audit=[]`

Yes, Signature is mandatory for every loan product.

[Close](action:exit_case)

### 9. PASS > next
`5388 ms, model_calls=1, meaning=next_step/embedding/0.83, audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

**Next step:** The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

[Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### 10. PASS > Ek minute, Priya Verma ka case check karo
`228 ms, model_calls=0, meaning=None, audit=['DOCUMENT_VERIFICATION:OK:documents.get,documents.checklist']`

CASE-69469C6809C3
- No documents have been uploaded on this case yet, so nothing has been **verified**.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [Why is PAN required for this application?](ask:Why%20is%20PAN%20required%20for%20this%20application?)

[Close](action:exit_case)

### 11. PASS > iska pending kya hai?
`424 ms, model_calls=0, meaning=pending_documents/embedding/0.90, audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

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
`149 ms, model_calls=0, meaning=None, audit=[]`

CASE-28006A0D7DEE · Rahul Sharma · FOS
Applicant: name **did not pass**: the documents disagree.
Next step: Upload the Signature.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

[Close](action:exit_case)

### 13. PASS > uska status change hua?
`5484 ms, model_calls=1, meaning=None, audit=['APPLICATION_STATUS:OK:application.get,documents.checklist']`

CASE-28006A0D7DEE
- The application has been created. Signature is still **pending**.

**Next step:** Ask "what is pending" to see the documents.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What should I do next?](ask:What%20should%20I%20do%20next?)

[Close](action:exit_case)

### 14. PASS > loan amount 6 lakh karo
`86 ms, model_calls=0, meaning=None, audit=[]`

Change Loan amount (Rs) of CASE-28006A0D7DEE from 500000 to 600000?

[Confirm](action:confirm_write?ref=edit-5951b6) · [Cancel](action:cancel_write?ref=edit-5951b6)

[Close](action:exit_case)

### 15. PASS > haan
`4215 ms, model_calls=1, meaning=None, audit=['UPDATE_CASE_FORM:OK:application.update']`

Updated Loan amount (Rs) of CASE-28006A0D7DEE to 600000.

[Close](action:exit_case)

### 16. PASS > loan amount kitna hai ab?
`649 ms, model_calls=0, meaning=loan_amount/embedding/0.79, audit=['APPLICANT_PROFILE:OK:application.get']`

You applied for **₹6,00,000**.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### 17. PASS > loan amount 7 lakh karo
`81 ms, model_calls=0, meaning=None, audit=[]`

Change Loan amount (Rs) of CASE-28006A0D7DEE from 600000 to 700000?

[Confirm](action:confirm_write?ref=edit-3e9c4c) · [Cancel](action:cancel_write?ref=edit-3e9c4c)

[Close](action:exit_case)

### 18. PASS > ruko, cancel karo
`4324 ms, model_calls=1, meaning=None, audit=[]`

Okay, nothing was changed.

[Close](action:exit_case)

### 19. PASS > yes
`182 ms, model_calls=0, meaning=None, audit=['ACKNOWLEDGEMENT:CONVERSATION:']`

Sure. Let me know if you'd like to check anything else.

- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

### 20. PASS > nahi mera matlab bank statement ka status
`324 ms, model_calls=0, meaning=document_status/followup/1.00, audit=['DOCUMENT_VERIFICATION:OK:documents.verification,documents.checklist']`

**Bank Statement** is **verified**. Its issuing authority has not been independently confirmed.

- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

### 21. PASS > Is this case ready for CPA?
`554 ms, model_calls=0, meaning=cpa_readiness/embedding/1.00, audit=['READINESS:OK:workflow.readiness,documents.checklist']`

CPA readiness: 4 of 12 checks passed.

**Applicant documents**
- Signature: **PENDING**, not uploaded yet. Fix: Upload the Signature.
- Income Proof: **PENDING**, not uploaded yet. Fix: Upload the Income Proof.
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
- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)

[Close](action:exit_case)

### 22. PASS > CPA pe move kar do
`228 ms, model_calls=0, meaning=None, audit=[]`

CASE-28006A0D7DEE can't move to CPA yet, these need to be fixed first:
Applicant documents
- Signature: PENDING, not uploaded yet. Fix: Upload the Signature.
- Income Proof: PENDING, not uploaded yet. Fix: Upload the Income Proof.
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
`409 ms, model_calls=0, meaning=knowledge_documents/embedding/0.90, audit=[]`

Required: PAN (Permanent Account Number), Address Proof (Driving Licence, Passport or Voter ID), Bank Statement, Signature.

### 24. PASS > can I upload aadhaar instead of pan?
`339 ms, model_calls=0, meaning=knowledge_documents/embedding/1.00, audit=[]`

No. Aadhaar cannot replace it: the PAN (Permanent Account Number) requirement accepts only PAN.

[Close](action:exit_case)

### 25. PASS > what is api
`38 ms, model_calls=0, meaning=None, audit=['GUARDRAIL_BLOCKED:BLOCKED:']`

I can't share details of the systems behind this assistant. I can help with your cases, documents, KYC (Know Your Customer) and the steps to move a case to CPA.

### 26. PASS > thanks
`325 ms, model_calls=0, meaning=small_talk/embedding/1.00, audit=[]`

Okay. Anything else?

[What is the KYC status?](ask:What%20is%20the%20KYC%20status?) · [What is pending?](ask:What%20is%20pending?)

[Close](action:exit_case)

### 27. PASS > Abhi tak kya kya pending hai, ek summary do
`579 ms, model_calls=0, meaning=pending_documents/embedding/0.87, audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

KYC (Know Your Customer) needs attention:
• **Name** mismatch: **PAN** (Permanent Account Number) shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature
- Income Proof

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant) · [Upload Signature](action:upload?doc=SIGNATURE&party=applicant) · [Upload Income Proof](action:upload?doc=INCOME_PROOF&party=applicant)

- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [Why is signature required for this application?](ask:Why%20is%20signature%20required%20for%20this%20application?)

[Close](action:exit_case)
