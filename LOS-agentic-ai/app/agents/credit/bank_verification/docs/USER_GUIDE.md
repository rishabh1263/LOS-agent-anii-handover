# User guide

Open <http://127.0.0.1:8010/bank-verification/>.

## Find a case

The screen never lists cases. Type the **application number** or the **case ID** and press **Search**. Only an exact
match opens; a partial number shows "No case found".

## The case

1. **Header:** applicant, product, application number, loan amount, stage, case ID, whether the Account Aggregator
   (AA) data was received, and the agency. Above it, the loan stage (Sourcing → CPA → Credit …).
2. **Account card** (like the Bank Verification tab):
   * Bank name, account number and account type, then the latest three transactions (date, amount, credit/debit).
   * Each value shows where it came from: *From AA*, *From statement*, *Entered* or *Agency confirmed*.
   * **AA received:** the values arrive filled. Press **Edit** to correct one.
   * **AA not received:** type the details, and upload the statement in the next card.
3. **Statement and agency:** upload the bank statement (PDF or CSV), view the statement on file, choose the agency.
4. Press **Run verification** in the bar at the bottom.
5. **Verification checks:** every check with its result and reason. Click a check to see exactly what was compared
   and what to do. The purple line on top says the next step.
6. **Summary** (right): the result in a few plain sentences, the score (how many checks passed), and every check in
   one line.

## Agency confirmation

1. Choose the agency, the branch visit status, and whether the branch confirmed the account belongs to the applicant.
2. Answer each field check: verified / mismatch / not verified.
3. Write the reason and any remarks, upload the verification report, choose **Positive**, **Negative** or **Save for
   review**, and press **Submit decision**.
   * A Positive without the needed confirmations comes back as *Needs review*, with the reasons.
   * A missing report or check answer is refused, with the list of what is missing.
4. **Fill sample** fills the agency answers for a quick walkthrough.

## Report and history

**Download PDF** or **Print view** for the verification report. The history lists every step with its time.

## Cases to try

| Application | Case ID | What it shows |
|---|---|---|
| APL-2026-10401 | BV-2026-001 | Everything matches (AA received) |
| APL-2026-10402 | BV-2026-002 | AA not received, statement uploaded, everything matches |
| APL-2026-10403 | BV-2026-003 | Account number / type typed wrong |
| APL-2026-10404 | BV-2026-004 | Latest transaction typed wrong |
| APL-2026-10405 | BV-2026-005 | Statement is a scan and could not be read |
| APL-2026-10406 | BV-2026-006 | Balances do not add up |
| APL-2026-10407 | BV-2026-007 | AA data and statement differ |
| APL-2026-10408 | BV-2026-008 | Statement already used in another case |
| APL-2026-10409 | BV-2026-009 | Branch confirmation pending |
| APL-2026-10410 | BV-2026-010 | Branch says the account is not the applicant's |
| APL-2026-10411 | BV-2026-011 | Agency report missing |
| APL-2026-10412 | BV-2026-012 | Many problems at once |

To start the cases again from fresh, run `python -m app.agents.credit.bank_verification.demo_data.seed`.
