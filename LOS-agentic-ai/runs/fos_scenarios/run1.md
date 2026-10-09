# FOS scenarios -- 31 turns, p50 2304 ms, p95 5098 ms, max 9644 ms
evidence: {"loan_amount_after": "600000", "stage_after": "FOS", "case_1": "CASE-8C4C0D642B4D", "case_2": "CASE-7AD1C658CE0F"}

## 1. [A] Bhai, aaj FOS mein kaafi applications pending hain. Pehle batao kis applicant par kaam karna chahiye?
`200 4403 ms meaning=None audit=[]`

Which case is this about? Pick one and I'll answer "Bhai, aaj FOS (Field Officer Sales) mein kaafi applications **pending** hain. Pehle batao kis applicant pa" for it.

You have 3 cases: 1 with a KYC (Know Your Customer) issue, 2 with documents pending.

| # | Case | Applicant | Stage | Status | |
|---|---|---|---|---|---|
| 1 | CASE-F1E9673AB8CB | Amit R. (APP-83E2398E1297) | FOS | Created · 4 docs pending | [Open](action:open_case?id=CASE-F1E9673AB8CB) |
| 2 | CASE-7AD1C658CE0F | Priya V. (APP-7C93F7843F4D) | FOS | Created · 4 docs pending | [Open](action:open_case?id=CASE-7AD1C658CE0F) |
| 3 | CASE-8C4C0D642B4D | Rahul S. (APP-DDA3DCBB3D64) | FOS | Created · KYC issue | [Open](action:open_case?id=CASE-8C4C0D642B4D) |

Showing 1-3 of 3 (newest first).
That is the whole list.

## 2. [B] Rahul Sharma ka case kholo
`200 2372 ms meaning=None audit=['DOCUMENTS_PENDING:OK:documents.get,documents.checklist']`

CASE-8C4C0D642B4D
Your KYC (Know Your Customer) verification needs attention:
• **Name** mismatch: **PAN** (Permanent Account Number) shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature

**Next step:** Upload these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 3. [B] Is applicant ka status batao.
`200 2210 ms meaning=applicant_name/model/0.74 audit=['APPLICANT_PROFILE:OK:applicant.get']`

CASE-8C4C0D642B4D
- We have the customer's name as Rahul Sharma.

You can ask:
- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)
- [What is pending?](ask:What%20is%20pending?)

[Close](action:exit_case)

## 4. [B] Kya pending hai?
`200 2325 ms meaning=None audit=['DOCUMENTS_PENDING:OK:documents.get,workflow.pending_items,documents.checklist']`

CASE-8C4C0D642B4D
Your KYC verification needs attention:
• **Name** mismatch: **PAN** shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature

**Next step:** Upload these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 5. [B] Ye pending kyun hai?
`200 2312 ms meaning=None audit=['DOCUMENTS_PENDING:OK:documents.get,workflow.pending_items,documents.checklist']`

CASE-8C4C0D642B4D
Your KYC verification needs attention:
• **Name** mismatch: **PAN** shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Signature

**Next step:** Upload these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 6. [B] Ab mujhe kya karna chahiye?
`200 2304 ms meaning=None audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

CASE-8C4C0D642B4D
**Next step:** The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

You can ask:
- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

## 7. [B] Iske liye document chahiye ya sirf clarification?
`200 2355 ms meaning=pending_documents/model/0.64 audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-8C4C0D642B4D
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
`200 2239 ms meaning=pending_documents/model/0.66 audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-8C4C0D642B4D
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
`200 13 ms meaning=None audit=['GUARDRAIL_BLOCKED:BLOCKED:']`

I can help with your own authorised application information, but I can't provide other customers' private data or internal system details.

## 10. [C] Priya Verma
`200 347 ms meaning=None audit=[]`

What would you like to know about priya verma?

1. [What is the applicant's name?](ask:What%20is%20the%20applicant%27s%20name%3F)
2. [Which stage is this case in?](ask:Which%20stage%20is%20this%20case%20in%3F)
3. [What is the status of this case?](ask:What%20is%20the%20status%20of%20this%20case%3F)

[Close](action:exit_case)

## 11. [C] Iska kya scene hai?
`200 2029 ms meaning=None audit=['APPLICATION_STATUS:OK:application.get,documents.checklist']`

CASE-8C4C0D642B4D
- The application has been created. Signature is still **pending**.

**Next step:** Want to see which documents are pending?

You can ask:
- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What should I do next?](ask:What%20should%20I%20do%20next?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

## 12. [C] Achha, ab pehle wale par wapas chalo.
`200 21 ms meaning=None audit=[]`

I can't find that applicant in your cases.

## 13. [C] Uska pending point kya tha?
`200 2322 ms meaning=pending_documents/model/0.66 audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-8C4C0D642B4D
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
`200 2511 ms meaning=None audit=['APPLICATION_STATUS:OK:application.get,documents.checklist']`

CASE-8C4C0D642B4D
- The application has been created. Signature is still **pending**.

**Next step:** Want to see which documents are pending?

You can ask:
- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What should I do next?](ask:What%20should%20I%20do%20next?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)

[Close](action:exit_case)

## 15. [D] Nahi, mera matlab bank statement se tha.
`200 2395 ms meaning=upload_document/model/0.68 audit=[]`

Tap below to upload the Bank Statement.

[Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 16. [D] Usko process kar do.
`200 9193 ms meaning=None audit=['UNKNOWN:UNSUPPORTED:']`

CASE-8C4C0D642B4D
I can help with the application details, its status and stage, the customer's documents, and what to do next. Which of these did you mean?

You can ask:
- [What are my application details?](ask:What%20are%20my%20application%20details?)
- [What is my application status?](ask:What%20is%20my%20application%20status?)
- [What documents are still needed?](ask:What%20documents%20are%20still%20needed?)

[Close](action:exit_case)

## 17. [D] Ruko, abhi action mat lena.
`200 9644 ms meaning=None audit=['CASE_SNAPSHOT:NOT_RECORDED:case.snapshot']`

CASE-8C4C0D642B4D
This is not recorded on the case.

You can ask:
- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

## 18. [D] Pehle sirf batao kya issue hai.
`200 2273 ms meaning=None audit=['DOCUMENT_VERIFICATION:OK:documents.get,documents.checklist']`

CASE-8C4C0D642B4D
- The customer's **PAN**, driving licence and bank statement **passed** document verification. Its issuing authority has not been independently confirmed.

You can ask:
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)
- [Why does the checklist depend on the employment type?](ask:Why%20does%20the%20checklist%20depend%20on%20the%20employment%20type?)
- [Co-applicant](ask:Co-applicant)

[Close](action:exit_case)

## 19. [E] Is applicant ke pending FOS checks complete karne ke liye next step le lo.
`200 194 ms meaning=None audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

CASE-8C4C0D642B4D
**Next step:** The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

You can ask:
- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is pending?](ask:What%20is%20pending?)

[Close](action:exit_case)

## 20. [E] loan amount 6 lakh karo
`200 33 ms meaning=None audit=[]`

Change Loan amount (Rs) of CASE-8C4C0D642B4D from 500000 to 600000?

[Confirm](action:confirm_write?ref=edit-eea686) · [Cancel](action:cancel_write?ref=edit-eea686)

[Close](action:exit_case)

## 21. [E] confirm
`200 23 ms meaning=None audit=['UPDATE_CASE_FORM:OK:application.update']`

Updated Loan amount (Rs) of CASE-8C4C0D642B4D to 600000.

[Close](action:exit_case)

## 22. [E/F] Kya is case ko next stage par bhej sakte hain?
`200 163 ms meaning=None audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

CASE-8C4C0D642B4D
**Next step:** The next step is to collect and upload the missing document: Income Proof. Also, wait for a reviewer to check the recorded issue.

You can ask:
- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)
- [What can be used as income proof?](ask:What%20can%20be%20used%20as%20income%20proof?)

[Close](action:exit_case)

## 23. [E] CPA pe move kar do
`200 29 ms meaning=None audit=[]`

CASE-8C4C0D642B4D can't move to CPA yet, these need to be fixed first:
- FOS (Field Officer Sales) requirements
- KYC (Know Your Customer) verification

[Collect from customer](ask:Collect%20from%20customer)

[Close](action:exit_case)

## 24. [F] Agar ye document missing hai toh ab kya karna hoga?
`200 3689 ms meaning=customer_message/model/0.62 audit=['OUT_OF_SCOPE:ROUTED:']`

CASE-8C4C0D642B4D
A message for the customer (not sent, copy or edit it):
> Hello, for your loan application please share: Income Proof, Signature. Thank you.

[Copy message](action:copy?ref=draft-1)

You can ask:
- [What is pending?](ask:What%20is%20pending?)
- [Why is it stuck?](ask:Why%20is%20it%20stuck?)

[Close](action:exit_case)

## 25. [F] Policy mein exactly kya requirement hai?
`200 5098 ms meaning=knowledge_documents/model/0.70 audit=[]`

Policy mein kya requirement hai, depend ke liye lender.

## 26. [D] Actually, abhi us task ko chhod do.
`200 3694 ms meaning=None audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

CASE-8C4C0D642B4D
**Next step:** Got it: the next step is to collect and upload the missing document: Income Proof. Also, wait for a reviewer to check the recorded issue.

You can ask:
- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)
- [What can be used as income proof?](ask:What%20can%20be%20used%20as%20income%20proof?)

[Close](action:exit_case)

## 27. [D] Maine galat applicant select kiya.
`200 19 ms meaning=None audit=[]`

I can't find that applicant in your cases.

## 28. [G] Jo tumne bataya, uska next step kya hai?
`200 2265 ms meaning=next_step/model/0.71 audit=['NEXT_ACTION:OK:workflow.next_action,documents.checklist']`

CASE-8C4C0D642B4D
**Next step:** The next step is to collect and upload the missing document: Income Proof. Also, wait for a reviewer to check the recorded issue.

You can ask:
- [Is this case ready for CPA?](ask:Is%20this%20case%20ready%20for%20CPA?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)
- [What can be used as income proof?](ask:What%20can%20be%20used%20as%20income%20proof?)

[Close](action:exit_case)

## 29. [G] Abhi tak kya kya pending hai, ek summary do.
`200 3453 ms meaning=pending_documents/model/0.66 audit=['PENDING_ITEMS:OK:workflow.pending_items,documents.checklist']`

CASE-8C4C0D642B4D
Your KYC (Know Your Customer) verification needs attention:
• **Name** mismatch: **PAN** (Permanent Account Number) shows "RAHUL SHARMA", Driving Licence shows "RAHUL SHARMA", Bank Statement shows "ROHIT VERMA"
Please upload the correct documents:
- PAN
- Driving Licence
- Bank Statement
Still pending:
- Income Proof
- Signature

**Next step:** Clear these to move the case to CPA.

[Upload PAN](action:upload?doc=PAN&party=applicant) · [Upload Driving Licence](action:upload?doc=DRIVING_LICENCE&party=applicant) · [Upload Bank Statement](action:upload?doc=BANK_STATEMENT&party=applicant)

[Close](action:exit_case)

## 30. [H-resume] Hum kis applicant par the? Uska kya pending tha?
`200 2057 ms meaning=applicant_name/model/0.67 audit=['APPLICANT_PROFILE:OK:applicant.get']`

CASE-8C4C0D642B4D
- The application is in the name of Rahul Sharma.

You can ask:
- [What is pending on this case?](ask:What%20is%20pending%20on%20this%20case?)
- [What is the KYC status?](ask:What%20is%20the%20KYC%20status?)
- [What is pending?](ask:What%20is%20pending?)

[Close](action:exit_case)

## 31. [H-other-officer] 
`200 - ms meaning=None audit=None`

Aapka abhi koi case nahi hai.

[Create New Case](action:new_case)
