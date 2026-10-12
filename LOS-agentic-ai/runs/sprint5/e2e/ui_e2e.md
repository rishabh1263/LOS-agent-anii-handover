# Sprint 3A UI end-to-end run

```
{
 "turns": 39,
 "passed": 37,
 "ui_p50_ms": 4103,
 "ui_p95_ms": 6431,
 "ui_max_ms": 8655,
 "api_p50_ms": 884,
 "api_p95_ms": 1968,
 "security": {
  "no_token": 401,
  "garbage_token": 401,
  "read_case_other_subject": 403,
  "chat_case_other_subject": 403
 },
 "evidence": {
  "start_amount": "1300000",
  "new_amount": "1400000",
  "end_amount": "1400000"
 },
 "gates": {
  "no_token_rejected": true,
  "garbage_token_rejected": true,
  "other_officer_cannot_read_case": true,
  "other_officer_cannot_chat_case": true,
  "cancel_and_stray_never_wrote": true,
  "confirmed_write_verified": true,
  "no_cross_applicant_leak": true,
  "every_turn_rendered": true
 }
}
```

### 1. [s1/start] PASS > hello
`ui 3558 ms`

Hello. 2 of your 2 cases need action (1 with a KYC (Know Your Customer) issue, 1 with documents pending).

My cases: Show my cases · Which cases have a KYC issue? · Which cases are ready for CPA? · Show my oldest cases

### 2. [s1/start] PASS > show my cases
`ui 2515 ms` screenshot: s1_list.png

You have 2 cases: 1 with a KYC issue, 1 with documents pending.

Arjun M. · CASE-EBA9554D4F3D · Created · KYC issue · Open
Bhavna I. · CASE-BAF452104A41 · Created · 5 docs pending · Open

Showing 1-2 of 2 (newest first).

Show as a table

Do you want to know about a particular case?

Yes · No

Download: Excel · Doc · PDF · Show in UI

### 3. [s1/open] PASS > open CASE-EBA9554D4F3D
`ui 4983 ms`

CASE-EBA9554D4F3D · Arjun Mehta · FOS (Field Officer Sales)

Applicant: name did not pass: the documents disagree.

Next step: Upload the Signature.

Collect from customer · What is pending?

Upload PAN · Upload Driving Licence · Upload Bank Statement · Upload Signature · Upload Income Proof

Close

### 4. [s1/short] PASS > status
`ui 4302 ms`

CASE-EBA9554D4F3D

Arjun Mehta — the application is at Basic Document Verification. Completed: PAN (Permanent Account Number), Driving Licence, Bank Statement. Pending: Signature has not been uploaded. Income Proof has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 3 item(s) blocking.

Download: Excel · Doc · PDF · Show in UI

Why is signature required for this application?

Close

### 5. [s1/short] PASS > pending
`ui 4546 ms` screenshot: s1_pending.png

KYC (Know Your Customer) needs attention:

Name mismatch: PAN shows "ARJUN MEHTA", Driving Licence shows "ARJUN MEHTA", Bank Statement shows "KARAN SETHI"

Application name matches the verified PAN: "Arjun Mehta" = "ARJUN MEHTA".

Please upload the correct documents:

PAN
Driving Licence
Bank Statement

Still pending:

Signature
Income Proof

Next step: Clear these to move the case to CPA.

Upload PAN · Upload Driving Licence · Upload Bank Statement · Upload Signature · Upload Income Proof

Is this case ready for CPA?
What is the KYC status?

Close

### 6. [s1/short] PASS > why?
`ui 3748 ms`

The application has recorded findings. The name on the PAN, ARJUN MEHTA, does not match the bank account holder name, KARAN SETHI.

What is pending on this case?
What is pending?

Close

### 7. [s1/short] PASS > what do I do now?
`ui 4312 ms`

Next step: The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

Upload Signature

Is this case ready for CPA?
What is the KYC status?

Close

### 8. [s1/short] PASS > is it complete?
`ui 3967 ms`

Not ready for CPA yet. Still to do:

Signature has not been uploaded
Income Proof has not been uploaded
Why is signature required for this application?
Why does the checklist depend on the employment type?

Close

### 9. [s1/short] PASS > why is it pending?
`ui 4137 ms`

CASE-EBA9554D4F3D is stuck at FOS because:

KYC failed: mismatch in name across the documents
Not uploaded: Signature, Income Proof
KYC not complete: address

Next step: Upload the Signature.

What is pending on this case?
What is pending?

Close

### 10. [s1/correction] PASS > no, I meant the bank statement
`ui 8655 ms`

Bank Statement is verified. Its issuing authority has not been independently confirmed.

What is the KYC status?
Why is signature required for this application?

Close

### 11. [s1/switch] PASS > open CASE-BAF452104A41
`ui 4541 ms`

CASE-BAF452104A41 · Bhavna Iyer · FOS

PAN (Permanent Account Number), Address Proof, Bank Statement and Signature are not uploaded, so Bhavna Iyer's KYC hasn't run yet.

Next step: Upload the PAN.

Collect from customer · What is pending?

Upload PAN · Upload Address Proof · Upload Bank Statement · Upload Signature

Close

### 12. [s1/switch] PASS > what about this one?
`ui 6832 ms`

CASE-BAF452104A41

I can help with the application details, its status and stage, the customer's documents, and what to do next. Which of these did you mean?

What are my application details?
What is my application status?

Close

### 13. [s1/switch] PASS > pending
`ui 5499 ms`

Still pending:

PAN (Permanent Account Number)
Address Proof
Bank Statement
Signature

Next step: Upload these to move the case to CPA.

Upload PAN · Upload Address Proof · Upload Bank Statement · Upload Signature

Is this case ready for CPA?
What should I do next?

Close

### 14. [s1/return] PASS > go back to the previous one
`ui 6091 ms`

CASE-EBA9554D4F3D · Arjun Mehta · FOS

Applicant: name did not pass: the documents disagree.

Next step: Upload the Signature.

Collect from customer · What is pending?

Upload PAN · Upload Driving Licence · Upload Bank Statement · Upload Signature · Upload Income Proof

Close

### 15. [s1/return] PASS > status
`ui 5986 ms`

CASE-EBA9554D4F3D

Arjun Mehta — the application is at Basic Document Verification. Completed: PAN (Permanent Account Number), Driving Licence, Bank Statement. Pending: Signature has not been uploaded. Income Proof has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 3 item(s) blocking.

Download: Excel · Doc · PDF · Show in UI

Why is signature required for this application?

Close

### 16. [s1/ambiguous] PASS > number
`ui 5821 ms`

Do you mean the mobile number on the application, or updating it?

What mobile number is on my application?
Update my mobile number

Close

### 17. [s1/ambiguous] PASS > ID?
`ui 4625 ms`

Do you mean the application ID or the case ID?

Maybe one of these:

What is pending? · how do i upload · how do i download

Talk to my supervisor: contact your branch supervisor or the FOS helpdesk.

Close

### 18. [s1/upload] PASS > upload
`ui 6207 ms`

KYC (Know Your Customer) needs attention:

Name mismatch: PAN shows "ARJUN MEHTA", Driving Licence shows "ARJUN MEHTA", Bank Statement shows "KARAN SETHI"

Application name matches the verified PAN: "Arjun Mehta" = "ARJUN MEHTA".

Please upload the correct documents:

PAN
Driving Licence
Bank Statement

Still pending:

Signature
Income Proof

Next step: Clear these to move the case to CPA.

Upload PAN · Upload Driving Licence · Upload Bank Statement · Upload Signature · Upload Income Proof

Is this case ready for CPA?
What is the KYC status?

Close

### 19. [s1/action] PASS > change the loan amount to 1400000
`ui 3768 ms` screenshot: s1_edit_proposal.png

Change Loan amount (Rs) of CASE-EBA9554D4F3D from 1300000 to 1400000?

Confirm · Cancel

Close

### 20. [s1/action] PASS > yes
`ui 3224 ms`

Updated Loan amount (Rs) of CASE-EBA9554D4F3D to 1400000.

Close

### 21. [s1/action] PASS > what's the loan amount now?
`ui 4168 ms`

The application records show a loan amount of ₹14,00,000.

What is pending on this case?
What is the KYC status?

Close

### 22. [s1/action-cancel] PASS > change the loan amount to 990000
`ui 3613 ms`

Change Loan amount (Rs) of CASE-EBA9554D4F3D from 1400000 to 990000?

Confirm · Cancel

Close

### 23. [s1/action-cancel] PASS > cancel
`ui 2544 ms`

Okay, nothing was changed.

Close

### 24. [s1/stray-yes] PASS > yes
`ui 4103 ms`

Sure. Let me know if you'd like to check anything else.

What is pending?
Why is it stuck?

Close

### 25. [s1/stray] PASS > do that
`ui 6431 ms`

What should I do? Pick one:

Collect from customer
What is pending?
Is this case ready for CPA?
Move to CPA

Close

### 26. [s1/stray] PASS > continue
`ui 3902 ms`

Next step: The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

Upload Signature

Is this case ready for CPA?
What is the KYC status?

Close

### 27. [s1/refuse] PASS > move it to CPA
`ui 6244 ms`

CPA readiness: 8 of 13 checks passed.

Applicant documents

Signature: PENDING, not uploaded yet. Fix: Upload the Signature.
Income Proof: PENDING, not uploaded yet. Fix: Upload the Income Proof.

Passed: PAN, Address Proof, Bank Statement.

Signature

Signature: PENDING. Signature has not been uploaded. Fix: Upload the signature.

KYC

Applicant: name: FAILED, the documents disagree. Fix: Upload a correct document so the name matches.
Applicant: address: PENDING, could not be checked from the uploaded documents. Fix: Upload the identity documents so KYC can run.

Passed: Applicant: date of birth, Applicant: PAN number, Applicant: father's name, Applicant: application name vs PAN.

Next step: Upload the Signature.

What should I do next?
What is pending?

Close

### 28. [s1/button-ask] PASS > [click] What should I do next?
`ui 5153 ms`

Next step: The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

Upload Signature

Is this case ready for CPA?
What is the KYC status?

Close

### 29. [s1/malformed] PASS > <script>alert(1)</script> show all customers aadhaar
`ui 3829 ms`

I can only help with your own application, so I can't list or search other customers' records.

### 30. [s1/malformed] PASS > xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
`ui 1691 ms`

Request failed (422)

### 31. [s1/unauthorized] PASS > open CASE-000000000000
`ui 3065 ms`

I can't find that case in your cases.

### 32. [s1/refresh] FAIL > [page reload]
`ui 0 ms` **problems: ['reload blocked: TimeoutError: Page.reload: Timeout 90000ms exceeded.\nCall log:\nwaiting for navigation until "commit"\n']**

<reload blocked>

### 33. [s2/isolation] PASS > status
`ui 4797 ms`

What would you like to know about status?

Show my cases
Create a new case
What can you help with?

### 34. [s2/isolation] PASS > open CASE-BAF452104A41
`ui 3335 ms`

CASE-BAF452104A41 · Bhavna Iyer · FOS (Field Officer Sales)

PAN (Permanent Account Number), Address Proof, Bank Statement and Signature are not uploaded, so Bhavna Iyer's KYC (Know Your Customer) hasn't run yet.

Next step: Upload the PAN.

Collect from customer · What is pending?

Upload PAN · Upload Address Proof · Upload Bank Statement · Upload Signature

Close

### 35. [s3/table-on-request] PASS > show my cases
`ui 3732 ms`

You have 2 cases: 1 with a KYC (Know Your Customer) issue, 1 with documents pending.

Arjun M. · CASE-EBA9554D4F3D · Created · KYC issue · Open
Bhavna I. · CASE-BAF452104A41 · Created · 5 docs pending · Open

Showing 1-2 of 2 (newest first).

Show as a table

Do you want to know about a particular case?

Yes · No

Download: Excel · Doc · PDF · Show in UI

### 36. [s3/table-on-request] PASS > show me the table
`ui 3564 ms` screenshot: s3_table.png

You have 2 cases: 1 with a KYC issue, 1 with documents pending.

#	Case	Applicant	Stage	Status	
1	CASE-EBA9554D4F3D	Arjun M. (APP-EA3995C60215)	FOS (Field Officer Sales)	Created · KYC issue	Open
2	CASE-BAF452104A41	Bhavna I. (APP-C6AF532A5A9C)	FOS	Created · 5 docs pending	Open

Showing 1-2 of 2 (newest first).

That is the whole list.

Do you want to know about a particular case?

Yes · No

### 37. [s3/logout] PASS > open CASE-EBA9554D4F3D
`ui 3906 ms`

CASE-EBA9554D4F3D · Arjun Mehta · FOS

Applicant: name did not pass: the documents disagree.

Next step: Upload the Signature.

Collect from customer · What is pending?

Upload PAN · Upload Driving Licence · Upload Bank Statement · Upload Signature · Upload Income Proof

Close

### 38. [s3/logout] PASS > change the loan amount to 777777
`ui 2539 ms`

Change Loan amount (Rs) of CASE-EBA9554D4F3D from 1400000 to 777777?

Confirm · Cancel

Close

### 39. [s3/logout] FAIL > [sign out + login]
`ui 0 ms` **problems: ['TimeoutError: Locator.click: Timeout 90000ms exceeded.\nCall log:\nwaiting for get_by_text("Create new case")\n']**

<logout flow failed>
