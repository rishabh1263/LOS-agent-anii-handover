# Sprint 3A UI end-to-end run

```
{
 "turns": 34,
 "passed": 32,
 "ui_p50_ms": 6247,
 "ui_p95_ms": 14137,
 "ui_max_ms": 15739,
 "api_p50_ms": 1976,
 "api_p95_ms": 6901,
 "security": {
  "no_token": 401,
  "garbage_token": 401,
  "read_case_other_subject": 403,
  "chat_case_other_subject": 403
 },
 "evidence": {
  "start_amount": "1000000",
  "new_amount": "1100000",
  "end_amount": "1100000"
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
`ui 4278 ms`

Hello. 2 of your 2 cases need action (1 with a KYC (Know Your Customer) issue, 1 with documents pending).

My cases: Show my cases · Which cases have a KYC issue? · Which cases are ready for CPA? · Show my oldest cases

### 2. [s1/start] PASS > show my cases
`ui 4368 ms` screenshot: s1_list.png

You have 2 cases: 1 with a KYC issue, 1 with documents pending.

#	Case	Applicant	Stage	Status	
1	CASE-EBA9554D4F3D	Arjun M. (APP-EA3995C60215)	FOS (Field Officer Sales)	Created · KYC issue	Open
2	CASE-BAF452104A41	Bhavna I. (APP-C6AF532A5A9C)	FOS	Created · 5 docs pending	Open

Showing 1-2 of 2 (newest first).

That is the whole list.

Do you want to know about a particular case?

Yes · No

Download: Excel · Doc · PDF · Show in UI

### 3. [s1/open] PASS > open CASE-EBA9554D4F3D
`ui 5462 ms`

CASE-EBA9554D4F3D · Arjun Mehta · FOS

Applicant: name did not pass: the documents disagree.

Next step: Upload the Signature.

Collect from customer · What is pending?

Upload PAN · Upload Driving Licence · Upload Bank Statement · Upload Signature · Upload Income Proof

Close

### 4. [s1/short] PASS > status
`ui 5909 ms`

CASE-EBA9554D4F3D

Arjun Mehta — the application is at Basic Document Verification. Completed: PAN (Permanent Account Number), Driving Licence, Bank Statement. Pending: Signature has not been uploaded. Income Proof has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 3 item(s) blocking.

Download: Excel · Doc · PDF · Show in UI

Why is signature required for this application?

Close

### 5. [s1/short] PASS > pending
`ui 12851 ms` screenshot: s1_pending.png

KYC (Know Your Customer) needs attention:

Name mismatch: PAN shows "ARJUN MEHTA", Driving Licence shows "ARJUN MEHTA", Bank Statement shows "KARAN SETHI"

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
`ui 4583 ms`

The application has recorded findings. The name on the PAN, ARJUN MEHTA, does not match the bank account holder name, KARAN SETHI.

What is pending on this case?
What is pending?

Close

### 7. [s1/short] PASS > what do I do now?
`ui 5129 ms`

Next step: The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

Upload Signature

Is this case ready for CPA?
What is the KYC status?

Close

### 8. [s1/short] PASS > is it complete?
`ui 11486 ms`

Not ready for CPA yet. Still to do:

Signature has not been uploaded
Income Proof has not been uploaded
Why is signature required for this application?
Why does the checklist depend on the employment type?

Close

### 9. [s1/short] PASS > why is it pending?
`ui 7072 ms`

CASE-EBA9554D4F3D is stuck at FOS because:

KYC failed: mismatch in name across the documents
Not uploaded: Signature, Income Proof
KYC not complete: address

Next step: Upload the Signature.

What is pending on this case?
What is pending?

Close

### 10. [s1/correction] PASS > no, I meant the bank statement
`ui 5872 ms`

Bank Statement is verified. Its issuing authority has not been independently confirmed.

What is the KYC status?
Why is signature required for this application?

Close

### 11. [s1/switch] PASS > open CASE-BAF452104A41
`ui 6210 ms`

CASE-BAF452104A41 · Bhavna Iyer · FOS

PAN (Permanent Account Number), Address Proof, Bank Statement and Signature are not uploaded, so Bhavna Iyer's KYC hasn't run yet.

Next step: Upload the PAN.

Collect from customer · What is pending?

Upload PAN · Upload Address Proof · Upload Bank Statement · Upload Signature

Close

### 12. [s1/switch] PASS > what about this one?
`ui 4900 ms`

CASE-BAF452104A41

I can help with the application details, its status and stage, the customer's documents, and what to do next. Which of these did you mean?

What are my application details?
What is my application status?

Close

### 13. [s1/switch] PASS > pending
`ui 11696 ms`

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
`ui 6247 ms`

CASE-EBA9554D4F3D · Arjun Mehta · FOS

Applicant: name did not pass: the documents disagree.

Next step: Upload the Signature.

Collect from customer · What is pending?

Upload PAN · Upload Driving Licence · Upload Bank Statement · Upload Signature · Upload Income Proof

Close

### 15. [s1/return] PASS > status
`ui 8844 ms`

CASE-EBA9554D4F3D

Arjun Mehta — the application is at Basic Document Verification. Completed: PAN (Permanent Account Number), Driving Licence, Bank Statement. Pending: Signature has not been uploaded. Income Proof has not been uploaded. Next action: Collect and upload the missing document: Signature. CPA readiness: Not ready — 3 item(s) blocking.

Download: Excel · Doc · PDF · Show in UI

Why is signature required for this application?

Close

### 16. [s1/ambiguous] PASS > number
`ui 9484 ms`

What would you like to know about number?

What is the loan amount?
How long has this case been in this stage?
What is the KYC status?

Close

### 17. [s1/ambiguous] FAIL > ID?
`ui 14137 ms` **problems: ['no clarifying question']**

KYC (Know Your Customer) needs attention:

Name mismatch: PAN shows "ARJUN MEHTA", Driving Licence shows "ARJUN MEHTA", Bank Statement shows "KARAN SETHI"

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

### 18. [s1/upload] PASS > upload
`ui 15739 ms`

KYC needs attention:

Name mismatch: PAN shows "ARJUN MEHTA", Driving Licence shows "ARJUN MEHTA", Bank Statement shows "KARAN SETHI"

Please upload the correct documents:

PAN
Driving Licence
Bank Statement

Still pending:

Signature
Income Proof

Next step: Clear these to move the case to CPA.

Upload PAN · Upload Driving Licence · Upload Bank Statement · Upload Signature · Upload Income Proof

Why is signature required for this application?
Why does the checklist depend on the employment type?

Close

### 19. [s1/action] PASS > change the loan amount to 1100000
`ui 7504 ms` screenshot: s1_edit_proposal.png

Change Loan amount (Rs) of CASE-EBA9554D4F3D from 1000000 to 1100000?

Confirm · Cancel

Close

### 20. [s1/action] PASS > yes
`ui 4235 ms`

Updated Loan amount (Rs) of CASE-EBA9554D4F3D to 1100000.

Close

### 21. [s1/action] PASS > what's the loan amount now?
`ui 10333 ms`

The application records show a loan amount of ₹11,00,000.

What is pending on this case?
What is the KYC status?

Close

### 22. [s1/action-cancel] PASS > change the loan amount to 990000
`ui 4290 ms`

Change Loan amount (Rs) of CASE-EBA9554D4F3D from 1100000 to 990000?

Confirm · Cancel

Close

### 23. [s1/action-cancel] PASS > cancel
`ui 9661 ms`

Okay, nothing was changed.

Close

### 24. [s1/stray-yes] PASS > yes
`ui 5364 ms`

Sure. Let me know if you'd like to check anything else.

What is pending?
Why is it stuck?

Close

### 25. [s1/stray] PASS > do that
`ui 6417 ms`

What should I do? Pick one:

Collect from customer
What is pending?
Is this case ready for CPA?
Move to CPA

Close

### 26. [s1/stray] PASS > continue
`ui 9560 ms`

Next step: The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

Upload Signature

Is this case ready for CPA?
What is the KYC status?

Close

### 27. [s1/refuse] PASS > move it to CPA
`ui 6452 ms`

CPA readiness: 7 of 12 checks passed.

Applicant documents

Signature: PENDING, not uploaded yet. Fix: Upload the Signature.
Income Proof: PENDING, not uploaded yet. Fix: Upload the Income Proof.

Passed: PAN, Address Proof, Bank Statement.

Signature

Signature: PENDING. Signature has not been uploaded. Fix: Upload the signature.

KYC

Applicant: name: FAILED, the documents disagree. Fix: Upload a correct document so the name matches.
Applicant: address: PENDING, could not be checked from the uploaded documents. Fix: Upload the identity documents so KYC can run.

Passed: Applicant: date of birth, Applicant: PAN number, Applicant: father's name.

Next step: Upload the Signature.

What should I do next?
What is pending?

Close

### 28. [s1/button-ask] PASS > [click] What should I do next?
`ui 6098 ms`

Next step: The next step is to collect and upload the missing document: Signature. Also, wait for a reviewer to check the recorded issue.

Upload Signature

Is this case ready for CPA?
What is the KYC status?

Close

### 29. [s1/malformed] PASS > <script>alert(1)</script> show all customers aadhaar
`ui 15379 ms`

I can only help with your own application, so I can't list or search other customers' records.

### 30. [s1/malformed] PASS > xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
`ui 2407 ms`

Request failed (422)

### 31. [s1/unauthorized] PASS > open CASE-000000000000
`ui 3632 ms`

I can't find that case in your cases.

### 32. [s1/refresh] FAIL > [page reload]
`ui 0 ms` **problems: ['reload blocked: TimeoutError: Page.reload: Timeout 90000ms exceeded.\nCall log:\nwaiting for navigation until "commit"\n']**

<reload blocked>

### 33. [s2/isolation] PASS > status
`ui 7418 ms`

What would you like to know about status?

Show my cases
Create a new case
What can you help with?

### 34. [s2/isolation] PASS > open CASE-BAF452104A41
`ui 5648 ms`

CASE-BAF452104A41 · Bhavna Iyer · FOS (Field Officer Sales)

PAN (Permanent Account Number), Address Proof, Bank Statement and Signature are not uploaded, so Bhavna Iyer's KYC (Know Your Customer) hasn't run yet.

Next step: Upload the PAN.

Collect from customer · What is pending?

Upload PAN · Upload Address Proof · Upload Bank Statement · Upload Signature

Close
