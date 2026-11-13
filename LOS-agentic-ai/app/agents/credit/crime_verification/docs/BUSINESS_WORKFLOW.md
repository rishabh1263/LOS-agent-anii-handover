# Business workflow

1. **Open the application.** The screen shows:
   - the header: applicant, application id, product, loan amount, stage, status;
   - the verification tabs, with **Criminal Record Check** selected;
   - the vendor (Crime Check);
   - the applicant and the co-applicant on the left, each with their latest status.
2. **Details.** The details come from the application, and each field shows "From application". If an officer edits
   a field, it shows "Edited".
   - The PAN shows masked.
   - "Enter full PAN" lets the officer type it. It must match the application.
   - Missing or invalid fields are flagged inline. A subject with incomplete details cannot be initiated.
3. **Select subjects.** Tick the applicant, the co-applicant, or both. Each person gets an independent result.
4. **Initiate Verification.** The screen validates first.
   - If something is wrong, it says what and where, and **no popup** opens.
   - If everything is valid, the **confirmation popup** opens: *"Confirm Applicant Details and Initiate Crime
     Verification?"*. It shows each selected person (name, father's name, DOB, address, masked PAN) and the warning.
5. **Cancel** closes the popup, and nothing is sent.
6. **Confirm & Initiate Verification** sends API 1, once, with a submission key.
   - While the request runs, the button shows "Initiating Verification…" and is disabled.
   - The server saves the confirmed snapshot, writes the audit ("details confirmed and verification initiated"),
     calls the provider for each person, and stores each result.
   - On success, the popup closes and the case reference and status show.
   - On failure, the popup stays open with the reason, and the details are kept.
7. **Results.** Each person's result is shown on its own:
   - **No Risk / Low Risk:** Completed. "No synthetic matches" never means "no record".
   - **Average / High / Very High:** Review required.
   - **Pending:** Processing. **Refresh status** asks the provider again.
   - **Failed:** the reason is shown, and Initiate again is a **retry**.
   - **Waived:** the waiver reason is shown, and **ReTrigger** goes through the same popup.
8. **Summary.** When all results are in, a plain-words summary appears. It is written by the local AI and checked
   first; until then, or if the AI is not available, the rules-written summary shows. It explains the results; it
   never decides.
9. **Report.** **Download report (PDF)** is available per person. It contains:
   - the demo notice;
   - the summary;
   - the subject (masked PAN);
   - the provider reference, risk, and findings by source and by matter type;
   - full coverage, the risk guide, and limitations.
10. **Acknowledge (API 2).** The officer records Accepted, Refer to credit or Decline recommended, with remarks when
    required. This never changes the provider's result.
11. **Running it again.** A person already Completed, Review required or Waived needs **ReTrigger**. A person still
    processing can never be sent twice.
12. **Audit.** Every step appears in the Activity timeline: confirmation, provider request, result or failure,
    refresh, report generated or downloaded, decision.

**For production** (not in this build):

- an authorised vendor adapter (`providers/`);
- the real LOS applications instead of the synthetic table;
- the confirmed risk policy (`review_required_risks`, decisions);
- `demo_mode: false` and the DSN in `.env`.
