# FOS scenarios -- 35 turns, p50 960 ms, p95 5019 ms, max 5789 ms
evidence: {"amount_after_confirm": "600000", "amount_after_hold": "600000", "loan_amount_after": "600000", "stage_after": "FOS", "case_1": "CASE-6060B8DC99B7", "case_2": "CASE-6FA8AEA36A81"}

## 1. [A] Bhai, aaj FOS mein kaafi applications pending hain. Pehle batao kis applicant par kaam karna chahiye?
`200 1250 ms meaning=work_queue/embedding/0.90 audit=[]`

3 of your cases match (needs action).

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-31228CE34F33 | Amit R. (APP-0788F43AC3C6) | FOS (Field Officer Sales) | Created · 4 docs **pending** | [Open](action:open_case?id=CASE-31228CE34F33) |
| 2 | CASE-6FA8AEA36A81 | Priya V. (APP-957D0B5EDB22) | FOS | Created · 4 docs pending | [Open](action:open_case?id=CASE-6FA8AEA36A81) |
| 3 | CASE-6060B8DC99B7 | Rahul S. (APP-B428A663118C) | FOS | Created · KYC (Know Your Customer) issue | [Open](action:open_case?id=CASE-6060B8DC99B7) |

Showing 1-3 of 3 (newest first).
That is the whole list.

Do you want to know about a particular case?
[Yes](ask:Yes) · [No](ask:No)

Download: [Excel](action:download_list?format=xlsx&q=filter:needs_action) · [Doc](action:download_list?format=docx&q=filter:needs_action) · [PDF](action:download_list?format=pdf&q=filter:needs_action) · [Show in UI](action:show_list_in_ui?q=filter:needs_action)

## 2. [B] Rahul Sharma ka case kholo
`200 3701 ms meaning=None audit=[]`

CASE-6060B8DC99B7 · Rahul Sharma · FOS
Applicant: name **did not pass**: the documents disagree.
Next step: Upload the signature.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

[Close](action:exit_case)

## 3. [B] Is applicant ka status batao.
`200 5735 ms meaning=case_status/embedding/0.99 audit=['FULL_SUMMARY:OK:applicant.360']`

CASE-6060B8DC99B7
- Rahul Sharma — the application is at Basic Document Verification. Completed: **PAN** (Permanent Account Number), Driving Licence, Bank Statement. **Pending**: Signature has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 1 item(s) blocking.

Download: [Excel](action:download?format=xlsx&case=CASE-6060B8DC99B7) · [Doc](action:download?format=docx&case=CASE-6060B8DC99B7) · [PDF](action:download?format=pdf&case=CASE-6060B8DC99B7) · [Show in UI](action:show_in_ui?case=CASE-6060B8DC99B7)

You can ask:
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

## 4. [B] Kya pending hai?
`200 2125 ms meaning=pending_documents/embedding/0.97 audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-6060B8DC99B7
Your KYC (Know Your Customer) verification needs attention:
• **Name** mismatch: **PAN** shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 5. [B] Ye pending kyun hai?
`200 1221 ms meaning=why_stuck/embedding/0.98 audit=['CASE_HISTORY:OK:application.get,workflow.pending_items,workflow.next_action']`

CASE-6060B8DC99B7
The records show the application is held because the details across documents disagree, so the case is held for a reviewer; and the signature is still outstanding, so the case is **not ready** for handoff. The next step is to collect and upload the signature.

You can ask:
- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

## 6. [B] Ab mujhe kya karna chahiye?
`200 1173 ms meaning=next_step/embedding/0.98 audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

CASE-6060B8DC99B7
**Next step:** The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

[Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

You can ask:
- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

## 7. [B] Iske liye document chahiye ya sirf clarification?
`200 960 ms meaning=pending_documents/embedding/0.99 audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-6060B8DC99B7
Your KYC verification needs attention:
• **Name** mismatch: **PAN** shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 8. [B] Agar document already upload hai toh?
`200 1213 ms meaning=pending_documents/embedding/0.98 audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-6060B8DC99B7
Your KYC verification needs attention:
• **Name** mismatch: **PAN** shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 9. [C] Ek minute, doosre applicant ka status dekhna hai.
`200 347 ms meaning=None audit=[]`

CASE-6060B8DC99B7 closed.

You have 3 cases: 1 with a KYC issue, 2 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-31228CE34F33 | Amit R. (APP-0788F43AC3C6) | FOS | Created · 4 docs pending | [Open](action:open_case?id=CASE-31228CE34F33) |
| 2 | CASE-6FA8AEA36A81 | Priya V. (APP-957D0B5EDB22) | FOS | Created · 4 docs pending | [Open](action:open_case?id=CASE-6FA8AEA36A81) |
| 3 | CASE-6060B8DC99B7 | Rahul S. (APP-B428A663118C) | FOS | Created · KYC issue | [Open](action:open_case?id=CASE-6060B8DC99B7) |

Showing 1-3 of 3 (newest first).
That is the whole list.

Do you want to know about a particular case?
[Yes](ask:Yes) · [No](ask:No)

Download: [Excel](action:download_list?format=xlsx&q=all) · [Doc](action:download_list?format=docx&q=all) · [PDF](action:download_list?format=pdf&q=all) · [Show in UI](action:show_list_in_ui?q=all)

## 10. [C] Priya Verma
`200 3538 ms meaning=None audit=[]`

CASE-6FA8AEA36A81 · Priya Verma · FOS
PAN (Permanent Account Number), Address Proof, Bank Statement and Signature are not uploaded, so Priya Verma's KYC hasn't run yet.
Next step: Upload the PAN.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload PAN](action:upload?doc=PAN&party=applicant)

[Close](action:exit_case)

## 11. [C] Iska kya scene hai?
`200 696 ms meaning=case_status/embedding/0.98 audit=['FULL_SUMMARY:OK:applicant.360']`

CASE-6FA8AEA36A81
- Priya Verma — the application is at Application Created. **Pending**: PAN (Permanent Account Number) has not been uploaded. Address Proof has not been uploaded. Bank Statement has not been uploaded. Signature has not been uploaded. Next action: Collect and upload the missing document: PAN. CPA readiness: Not ready — 4 item(s) blocking.

Download: [Excel](action:download?format=xlsx&case=CASE-6FA8AEA36A81) · [Doc](action:download?format=docx&case=CASE-6FA8AEA36A81) · [PDF](action:download?format=pdf&case=CASE-6FA8AEA36A81) · [Show in UI](action:show_in_ui?case=CASE-6FA8AEA36A81)

You can ask:
- [Why is PAN required for this application?](ask:Why%20is%20PAN%20required%20for%20this%20application?)

[Close](action:exit_case)

## 12. [C] Achha, ab pehle wale par wapas chalo.
`200 313 ms meaning=None audit=[]`

CASE-6060B8DC99B7 · Rahul Sharma · FOS
Applicant: name **did not pass**: the documents disagree.
Next step: Upload the signature.

[Collect from customer](ask:Collect%20from%20customer) · [What is pending?](ask:What%20is%20pending?)

[Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

[Close](action:exit_case)

## 13. [C] Uska pending point kya tha?
`200 783 ms meaning=pending_documents/embedding/0.98 audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-6060B8DC99B7
Your KYC (Know Your Customer) verification needs attention:
• **Name** mismatch: **PAN** (Permanent Account Number) shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 14. [C] Kya uska status change hua?
`200 5019 ms meaning=None audit=['APPLICATION_STATUS:OK:application.get,documents.checklist']`

CASE-6060B8DC99B7
- The application has been created. Signature is still **pending**.

**Next step:** Want to see which documents are pending?

You can ask:
- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What should I do next?](ask:What%20should%20I%20do%20next?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

## 15. [D] Nahi, mera matlab bank statement se tha.
`200 610 ms meaning=document_status/followup/1.00 audit=['DOCUMENT_VERIFICATION:OK:documents.verification,documents.checklist']`

CASE-6060B8DC99B7
- **Bank Statement** is **verified**. Its issuing authority has not been independently confirmed.

You can ask:
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)
- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

## 16. [D] Usko process kar do.
`200 440 ms meaning=ambiguous_action/embedding/0.99 audit=[]`

What should I do? Pick one:

1. [Collect from customer](ask:Collect%20from%20customer)
2. [What is pending?](ask:What%20is%20pending%3F)
3. [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA%3F)
4. [Move to CPA](ask:Move%20to%20CPA)

[Close](action:exit_case)

## 17. [D] Ruko, abhi action mat lena.
`200 385 ms meaning=hold_action/embedding/0.97 audit=[]`

Okay, I won't take any action. Nothing was **pending**, so nothing has changed. Ask me anything about the case.

[Close](action:exit_case)

## 18. [D] Pehle sirf batao kya issue hai.
`200 1091 ms meaning=why_stuck/embedding/0.99 audit=['CASE_HISTORY:OK:application.get,workflow.pending_items,workflow.next_action']`

CASE-6060B8DC99B7
The records show the application is held because the details across documents disagree, so the case is held for a reviewer; and the signature is still outstanding, so the case is **not ready** for handoff. The next step is to collect and upload the signature.

You can ask:
- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

## 19. [E] Is applicant ke pending FOS checks complete karne ke liye next step le lo.
`200 985 ms meaning=next_step/embedding/0.85 audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

CASE-6060B8DC99B7
**Next step:** The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

[Upload Signature](action:upload?doc=SIGNATURE&party=applicant)

You can ask:
- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

## 20. [E] loan amount 6 lakh karo
`200 162 ms meaning=None audit=[]`

Change Loan amount (Rs) of CASE-6060B8DC99B7 from 500000 to 600000?

[Confirm](action:confirm_write?ref=edit-aacc6b) · [Cancel](action:cancel_write?ref=edit-aacc6b)

[Close](action:exit_case)

## 21. [E] confirm
`200 417 ms meaning=command/embedding/1.00 audit=['UPDATE_CASE_FORM:OK:application.update']`

Updated Loan amount (Rs) of CASE-6060B8DC99B7 to 600000.

[Close](action:exit_case)

## 22. [D/E-hold] loan amount 7 lakh karo
`200 153 ms meaning=None audit=[]`

Change Loan amount (Rs) of CASE-6060B8DC99B7 from 600000 to 700000?

[Confirm](action:confirm_write?ref=edit-3203e2) · [Cancel](action:cancel_write?ref=edit-3203e2)

[Close](action:exit_case)

## 23. [D/E-hold] Ruko, abhi action mat lena.
`200 351 ms meaning=hold_action/embedding/0.97 audit=[]`

Okay, I have not done anything. The **pending** change was dropped, so nothing was written. Tell me when you want to continue.

[Close](action:exit_case)

## 24. [C-isolation] Priya ka pending kya hai?
`200 410 ms meaning=None audit=['DOCUMENTS_PENDING:OK:documents.get,workflow.pending_items,documents.checklist']`

CASE-6FA8AEA36A81
Still pending:
- **PAN** (Permanent Account Number)
- **Address Proof**
- Bank Statement
- Signature

**Next step:** Upload these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 25. [E/F] Kya is case ko next stage par bhej sakte hain?
`200 1058 ms meaning=cpa_readiness/embedding/0.99 audit=['READINESS:OK:workflow.readiness,documents.checklist']`

CASE-6FA8AEA36A81
CPA readiness: 1 of 10 checks passed.

**Applicant documents**
- PAN: **PENDING**, not uploaded yet. Fix: Upload the PAN.
- Address Proof: **PENDING**, not uploaded yet. Fix: Upload the Address Proof.
- Bank Statement: **PENDING**, not uploaded yet. Fix: Upload the Bank Statement.

**Signature**
- Signature: **PENDING**. Signature has not been uploaded.. Fix: Upload the signature.

**KYC**
- Applicant: **PENDING**. KYC has not run for this party. Fix: Upload the identity documents so KYC can run.

**Next step:** Upload the PAN.

You can ask:
- [What should I do next?](ask:What%20should%20I%20do%20next?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)
- [Why is PAN required for this application?](ask:Why%20is%20PAN%20required%20for%20this%20application?)

[Close](action:exit_case)

## 26. [E] CPA pe move kar do
`200 215 ms meaning=None audit=[]`

CASE-6FA8AEA36A81 can't move to CPA yet, these need to be fixed first:
- FOS (Field Officer Sales) requirements
- KYC (Know Your Customer) verification

[Collect from customer](ask:Collect%20from%20customer)

[Close](action:exit_case)

## 27. [F] Agar ye document missing hai toh ab kya karna hoga?
`200 1060 ms meaning=next_step/embedding/0.99 audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

CASE-6FA8AEA36A81
**Next step:** The next step is to collect and upload the missing document: PAN.

[Upload PAN](action:upload?doc=PAN&party=applicant)

You can ask:
- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

## 28. [F] Policy mein exactly kya requirement hai?
`200 978 ms meaning=cpa_readiness/embedding/0.98 audit=['READINESS:OK:workflow.readiness,documents.checklist']`

CASE-6FA8AEA36A81
CPA readiness: 1 of 10 checks passed.

**Applicant documents**
- PAN: **PENDING**, not uploaded yet. Fix: Upload the PAN.
- Address Proof: **PENDING**, not uploaded yet. Fix: Upload the Address Proof.
- Bank Statement: **PENDING**, not uploaded yet. Fix: Upload the Bank Statement.

**Signature**
- Signature: **PENDING**. Signature has not been uploaded.. Fix: Upload the signature.

**KYC**
- Applicant: **PENDING**. KYC has not run for this party. Fix: Upload the identity documents so KYC can run.

**Next step:** Upload the PAN.

You can ask:
- [What should I do next?](ask:What%20should%20I%20do%20next?)
- [Why is PAN required for this application?](ask:Why%20is%20PAN%20required%20for%20this%20application?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

## 29. [D] Actually, abhi us task ko chhod do.
`200 387 ms meaning=hold_action/embedding/0.98 audit=[]`

Okay, I won't take any action. Nothing was **pending**, so nothing has changed. Ask me anything about the case.

[Close](action:exit_case)

## 30. [G] Jo tumne bataya, uska next step kya hai?
`200 1204 ms meaning=next_step/embedding/0.98 audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

CASE-6FA8AEA36A81
**Next step:** The next step is to collect and upload the missing document: PAN.

[Upload PAN](action:upload?doc=PAN&party=applicant)

You can ask:
- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)

[Close](action:exit_case)

## 31. [G] Abhi tak kya kya pending hai, ek summary do.
`200 779 ms meaning=pending_documents/embedding/0.86 audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-6FA8AEA36A81
Still pending:
- **PAN**
- **Address Proof**
- Bank Statement
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 32. [B] Agar document already upload hai toh?
`200 864 ms meaning=pending_documents/embedding/0.98 audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-6FA8AEA36A81
Still pending:
- **PAN**
- **Address Proof**
- Bank Statement
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 33. [H-resume] Hum kis applicant par the? Uska kya pending tha?
`200 5789 ms meaning=None audit=['DOCUMENTS_PENDING:OK:documents.get,documents.checklist']`

CASE-6FA8AEA36A81
Still pending:
- **PAN**
- **Address Proof**
- Bank Statement
- Signature

**Next step:** Upload these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Address Proof](action:upload?doc=ADDRESS_PROOF&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 34. [D] Maine galat applicant select kiya.
`200 315 ms meaning=None audit=[]`

CASE-6FA8AEA36A81 closed.

You have 3 cases: 1 with a KYC issue, 2 with documents **pending**.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-6060B8DC99B7 | Rahul S. (APP-B428A663118C) | FOS | Created · KYC issue | [Open](action:open_case?id=CASE-6060B8DC99B7) |
| 2 | CASE-31228CE34F33 | Amit R. (APP-0788F43AC3C6) | FOS | Created · 4 docs pending | [Open](action:open_case?id=CASE-31228CE34F33) |
| 3 | CASE-6FA8AEA36A81 | Priya V. (APP-957D0B5EDB22) | FOS | Created · 4 docs pending | [Open](action:open_case?id=CASE-6FA8AEA36A81) |

Showing 1-3 of 3 (newest first).
That is the whole list.

Do you want to know about a particular case?
[Yes](ask:Yes) · [No](ask:No)

Download: [Excel](action:download_list?format=xlsx&q=all) · [Doc](action:download_list?format=docx&q=all) · [PDF](action:download_list?format=pdf&q=all) · [Show in UI](action:show_list_in_ui?q=all)

## 35. [H-other-officer] 
`200 - ms meaning=None audit=None`

Aapka abhi koi case nahi hai.

[Create New Case](action:new_case)
