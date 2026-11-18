# Business rules

Every limit and sentence is in config (`app/config/credit_underwriting/bank_verification*.yaml`). The status is
**DEMO_DUMMY / UNCONFIRMED**: the owner confirms or changes the values there, with no code change.

Statuses:

* `PASS`: checked and fine.
* `FAIL`: a confirmed mismatch.
* `REVIEW_REQUIRED`: suspicious, inconclusive or missing evidence, so a person looks.
* `NOT_APPLICABLE`: the rule had nothing to check. **It is never counted as a pass.**

## R1 Bank details matching

The bank name, account number and account type on the Leviosa form are compared with the statement header.

* Names and types are compared after folding case and spacing. Account-type synonyms are in config `account_types`
  ("SB" = SAVINGS).
* The account number is compared exactly; only whitespace is removed. A leading zero or a letter always counts.
* A masked statement number (`XXXX4051`) can only confirm its visible digits, so the result is REVIEW_REQUIRED.
* Any mismatch is FAIL. A missing value on either side is REVIEW_REQUIRED (missing evidence, not a failure).

## R2 Latest three transactions

* Each form row must be one of the statement's latest transactions with the **same date, amount and debit/credit
  side**.
* "Latest" is by transaction date and then print order; a newest-first statement is turned round. It is never "the
  last three lines of the PDF".
* If the 3rd and 4th latest fall on the same day, either one is accepted.
* A row the statement does not have is FAIL, with what differs (date / amount / side).
* A blank or incomplete row is REVIEW_REQUIRED. Nothing is guessed or invented. A statement with fewer transactions is
  handled as it is.

## R3 Statement integrity

* **Readable:** the text layer and the extraction status. A scan, password-protected or damaged file is
  REVIEW_REQUIRED, with the action "ask for a readable statement / enter manually".
* **Not a statement at all:** a CSV without the needed columns is FAIL.
* **Signals** (all REVIEW_REQUIRED, never proof of tampering):
  * an editing or conversion tool in the PDF producer/creator (config list);
  * modified more than 24 h after creation;
  * a transaction outside the printed period;
  * a partly read statement.

## R4 Account ownership

* Only the agency's branch confirmation decides:
  * CONFIRMED gives PASS;
  * NOT_CONFIRMED gives FAIL;
  * pending or none gives REVIEW_REQUIRED.
* The holder name on the statement is shown as **supporting evidence only** and flags a difference. A statement can
  never prove ownership on its own.

## R5 Continuity and balance

* Opening + credits − debits must equal the closing balance (Decimal, configured tolerance 0.00).
* Each printed running balance must follow from the previous one.
* A reversal is its own credit or debit row.
* Nothing to check gives NOT_APPLICABLE.
* Running balances only, with no opening/closing, gives REVIEW_REQUIRED.
* A break gives FAIL, naming the row.

## R6 AA vs uploaded statement

* Runs only when both are present; otherwise NOT_APPLICABLE.
* Compares bank, account number, type, IFSC, holder, opening/closing balance, and the transactions over the common
  period, naming each one that only one source has.

## R7 Duplicate / reuse

* **EXACT:** the same SHA-256 is in another case.
* **SIMILAR:** the same account and transactions (content fingerprint), or the same account with 3 or more identical
  transactions.
* Either gives REVIEW_REQUIRED. It is a reason to look, never "fraud". The same file re-uploaded in the same case is
  not a duplicate.

## R8 Risk assessment and report

* **Status:** FAIL if any rule failed, else REVIEW_REQUIRED if any needs a person, else PASS. All NOT_APPLICABLE gives
  NOT_APPLICABLE.
* **Flags:** sorted by severity. Severities are in the wording config.
* **Recommended action:** the most urgent, in this order:
  1. ask for a readable statement;
  2. correct or explain a mismatch;
  3. agency review;
  4. await ownership;
  5. escalate negative evidence (always, when R4 failed);
  6. ready for finalization.
* **"x of y applicable checks passed"** counts rules. **It is not a probability of fraud or ownership.**

## Final decision (API 2)

### Mandatory evidence

For POSITIVE or NEGATIVE, a missing item is refused with 422 and nothing is decided:

* every checklist item answered;
* a rationale of at least 20 characters;
* the verification report.

A REVIEW_REQUIRED save is an interim update and needs none of these.

### NEGATIVE

Allowed only with a recorded negative finding: ownership NOT_CONFIRMED, or a checklist MISMATCH. Otherwise the result
is REVIEW_REQUIRED, "a negative needs a recorded finding".

### POSITIVE

Needs **all** of the following:

* ownership CONFIRMED;
* branch visit COMPLETED;
* every checklist item VERIFIED;
* a readable statement on the case;
* no open FAIL in R1, R2, R3, R5 or R6;
* every rule left at REVIEW_REQUIRED resolved by the checklist items that cover it (config `review_resolved_by`);
* no negative finding.

Anything short gives REVIEW_REQUIRED, with every reason listed. **Passing comparisons alone never make a POSITIVE.
PENDING never becomes POSITIVE.**

### After the decision

The rules are re-run with the agency's ownership outcome, so the report shows the evidence the decision used. A final
case is not processed or finalized again; the identical finalize request replays.

## Safety

* Money is exact Decimal.
* Account numbers leave the API masked: in rule results, the form view and the report.
* Uploads are checked by content and size.
* Roles are enforced on the server.
* Every step is audited.
* No model approves or rejects anything. The AI summary only explains, and its text is checked: figures only from the
  results, no banned words, no field names or rule codes.

## AA received AND a statement uploaded (2026-10-28, Sahil Sir)

Each check does one job; what the form is checked against comes from config `reference_source`.

| Check | AA received | AA not received |
|---|---|---|
| R1 Bank details, R2 Latest transactions, R4 holder name | form ↔ **AA data** (the statement when no AA data) | form ↔ statement |
| R6 AA data ↔ statement | bank, account number, type, IFSC, holder, opening / closing balance, every transaction in the period both cover | not applicable |
| R3 integrity, R7 reuse, R5 continuity | on the uploaded statement; R3 / R7 are not applicable in an AA-only run | on the statement |

- **AA received, no statement:** the run is AA-only (`statement_source: NONE`). It is never refused for "no statement
  on file".
- **AA received, statement uploaded:** the file is read and kept at upload. The form stays as the AA data filled it.
  At the run, R6 names every difference, e.g. "only in AA: 21-09-2026 ₹1,240.50 debit | only in statement: …".
- The results table shows which source each value was compared with ("AA data" / "Statement").
