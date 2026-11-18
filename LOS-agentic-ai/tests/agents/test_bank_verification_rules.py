"""
BANK VERIFICATION AGENT -- the eight rules one by one, on hand-built synthetic evidence (no files, no store).
app/agents/credit/bank_verification/rules/.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from app.agents.credit.bank_verification.rules import (account_ownership, balance_validation, bank_details,
                                                       duplicate_detection, risk_assessment, run_all,
                                                       source_crosscheck, statement_integrity,
                                                       transaction_matching)
from app.agents.credit.bank_verification.rules.base import Evidence, PriorDocument
from app.agents.credit.bank_verification.schemas import (BankDetails, DocumentFacts, EnteredDetails,
                                                         EnteredTransaction, ExtractionStatus, Mode, OwnershipStatus,
                                                         RuleStatus, StatementData, Transaction, money, rupees)
from app.agents.credit.bank_verification.services import transaction_service

PASS, FAIL, REVIEW, NA = (RuleStatus.PASS, RuleStatus.FAIL, RuleStatus.REVIEW_REQUIRED, RuleStatus.NOT_APPLICABLE)


def tx(day: int, month: int, amount: str, side: str, balance: str | None = None, seq: int = 0) -> Transaction:
    return Transaction(date=date(2026, month, day), amount=amount, direction=side, balance=balance, seq=seq)


def statement(**over) -> StatementData:
    rows = [tx(1, 7, "50000", "CREDIT", "60000.00", 0), tx(5, 7, "12000", "DEBIT", "48000.00", 1),
            tx(9, 7, "2500.50", "DEBIT", "45499.50", 2), tx(20, 7, "1000", "CREDIT", "46499.50", 3)]
    base = dict(details=BankDetails(bank_name="Meridian Trust Bank", account_number="99901020304051",
                                    account_type="Savings", ifsc="MRTB0DEM051", account_holder="Test Person"),
                transactions=rows, opening_balance="10000.00", closing_balance="46499.50",
                period_start=date(2026, 7, 1), period_end=date(2026, 7, 31))
    return StatementData(**{**base, **over})


def form_from(data: StatementData, **over) -> EnteredDetails:
    filled = transaction_service.prefill(data, "MANUAL").model_dump()
    filled.update(over)
    return EnteredDetails.model_validate(filled)


def facts(**over) -> DocumentFacts:
    base = dict(file_name="s.pdf", kind="PDF", size=1000, sha256="a" * 64, fingerprint="f" * 64, pages=1,
                pages_with_text=1, extraction_status=ExtractionStatus.COMPLETE,
                metadata={"producer": "Bank Statement Engine", "creationDate": "D:20261001090000+05'30'",
                          "modDate": "D:20261001090000+05'30'"})
    return DocumentFacts(**{**base, **over})


def evidence(**over) -> Evidence:
    s = over.pop("statement", statement())
    base = dict(case_id="BV-T-1", mode=Mode.AA_FAILURE, applicant_name="Test Person",
                entered=over.pop("entered", form_from(s) if s else EnteredDetails()), statement=s, facts=facts())
    return Evidence(**{**base, **over})


# ---- money -----------------------------------------------------------------------------------------------------------
def test_money_is_exact_decimal_never_float():
    assert money("1,25,979.25") == D("125979.25") and money(0.1) == D("0.10") and money("") is None
    with pytest.raises(ValueError):
        money("twelve")
    assert rupees("125979.25") == "₹1,25,979.25"


def test_a_transaction_amount_must_be_positive():
    with pytest.raises(ValueError):
        tx(1, 7, "0", "CREDIT")


# ---- R1 --------------------------------------------------------------------------------------------------------------
def test_r1_passes_when_details_match_after_case_space_and_synonyms():
    s = statement()
    ev = evidence(statement=s, entered=form_from(s, bank_name="  MERIDIAN trust   bank ", account_type="SB"))
    assert bank_details.run(ev).status is PASS


def test_r1_fails_on_one_digit_and_names_the_field():
    s = statement()
    r = bank_details.run(evidence(statement=s, entered=form_from(s, account_number="99901020304015")))
    assert r.status is FAIL and "account number" in r.reason
    assert "99901020304015" not in str(r.model_dump())          # the number is never shown in full


def test_r1_never_normalises_away_a_leading_zero():
    s = statement(details=BankDetails(bank_name="B", account_number="0123456789", account_type="Savings"))
    assert bank_details.run(evidence(statement=s, entered=form_from(s, account_number="123456789"))).status is FAIL


def test_r1_masked_statement_confirms_only_the_visible_digits():
    s = statement(details=BankDetails(bank_name="Meridian Trust Bank", account_number="XXXXXXXXXX4051",
                                      account_type="Savings"))
    r = bank_details.run(evidence(statement=s, entered=form_from(statement())))
    assert r.status is REVIEW and "last 4 digits" in str(r.comparisons)


def test_r1_without_a_statement_is_missing_evidence_not_a_failure():
    r = bank_details.run(evidence(statement=None, entered=EnteredDetails(bank_name="X")))
    assert r.status is REVIEW and r.missing_evidence


# ---- R2 --------------------------------------------------------------------------------------------------------------
def test_r2_matches_the_latest_three_by_date_not_by_print_order():
    s = statement()
    newest_first = statement(transactions=[t.model_copy(update={"seq": 3 - t.seq}) for t in reversed(s.transactions)])
    latest = transaction_service.latest(newest_first.transactions)
    assert [t.date.day for t in latest.rows] == [20, 9, 5]
    assert transaction_matching.run(evidence(statement=newest_first, entered=form_from(s))).status is PASS


def test_r2_fails_on_amount_or_side_and_says_what_differs():
    s = statement()
    form = form_from(s)
    form.transactions[1] = form.transactions[1].model_copy(update={"amount": D("2500.00")})
    r = transaction_matching.run(evidence(statement=s, entered=form))
    assert r.status is FAIL and any(c.note == "the amount differs" for c in r.comparisons)


def test_r2_a_blank_row_is_missing_input_never_guessed():
    s = statement()
    form = form_from(s)
    form.transactions[2] = EnteredTransaction()
    r = transaction_matching.run(evidence(statement=s, entered=form))
    assert r.status is REVIEW and "Transaction 3 (not entered on the form)" in r.missing_evidence


def test_r2_same_day_boundary_accepts_either_transaction_of_that_day():
    rows = [tx(1, 7, "100", "CREDIT", seq=0), tx(2, 7, "200", "DEBIT", seq=1), tx(3, 7, "300", "DEBIT", seq=2),
            tx(3, 7, "400", "DEBIT", seq=3), tx(4, 7, "500", "CREDIT", seq=4)]
    s = statement(transactions=rows, opening_balance=None, closing_balance=None)
    form = EnteredDetails(transactions=[EnteredTransaction(date=date(2026, 7, d), amount=a, direction=side)
                                        for d, a, side in ((4, "500", "CREDIT"), (3, "400", "DEBIT"), (3, "300", "DEBIT"))])
    assert transaction_matching.run(evidence(statement=s, entered=form)).status is PASS


def test_r2_fewer_transactions_on_the_statement_are_never_invented():
    s = statement(transactions=[tx(1, 7, "50000", "CREDIT")])
    form = form_from(s)
    r = transaction_matching.run(evidence(statement=s, entered=form))
    assert r.status is PASS and len([c for c in r.comparisons if c.left]) == 1


# ---- R3 --------------------------------------------------------------------------------------------------------------
def test_r3_clean_file_passes():
    assert statement_integrity.run(evidence()).status is PASS


@pytest.mark.parametrize("meta, code", [
    ({"producer": "iLovePDF"}, "EDITING_TOOL_IN_METADATA"),
    ({"creationDate": "D:20261001090000+05'30'", "modDate": "D:20261010090000+05'30'"}, "MODIFIED_AFTER_CREATION"),
])
def test_r3_metadata_signals_are_review_never_fraud(meta, code):
    r = statement_integrity.run(evidence(facts=facts(metadata={**facts().metadata, **meta})))
    assert r.status is REVIEW and code in {f.code for f in r.risk_flags} and "fraud" not in r.reason.lower()


def test_r3_unreadable_is_review_and_invalid_is_fail():
    unreadable = statement_integrity.run(evidence(statement=None, facts=facts(
        extraction_status=ExtractionStatus.UNREADABLE, pages_with_text=0, notes=["no text layer"])))
    invalid = statement_integrity.run(evidence(statement=None, facts=facts(
        kind="CSV", extraction_status=ExtractionStatus.INVALID, notes=["missing columns"])))
    assert unreadable.status is REVIEW and invalid.status is FAIL


def test_r3_transaction_outside_the_period_is_flagged():
    s = statement(period_end=date(2026, 7, 15))
    assert statement_integrity.run(evidence(statement=s)).status is REVIEW


@pytest.mark.parametrize("entered, printed, same", [
    ("Axis Bank", "AXIS BANK LTD", True), ("Axis Bank", "Axis Bank Limited", True), ("SBI", "State Bank of India", True),
    ("Canara Bank", "CNRB", True), ("Punjab & Sind Bank", "Punjab and Sind Bank", True),
    ("Bank of India", "Union Bank of India", False), ("HDFC Bank", "ICICI Bank", False),
])
def test_r1_one_bank_written_many_ways_matches_and_two_banks_never_do(entered, printed, same):
    """Audit 2026-10-26: "Ltd", "Limited", a short name or the IFSC prefix do not make another bank (config bank_names)."""
    s = statement(details=BankDetails(bank_name=printed, account_number="99901020304051", account_type="Savings"))
    out = bank_details.run(evidence(statement=s, entered=form_from(s, bank_name=entered)))
    assert out.status is (PASS if same else FAIL)


def test_r2_never_passes_on_rows_from_a_statement_whose_end_was_not_read():
    """Audit 2026-10-26: the last page lost -> older rows would look "latest". The balance chain is whole but stops
    short of the printed closing balance, so no row can be confirmed -- REVIEW, never PASS (and never a false FAIL)."""
    full = statement()
    cut = statement(transactions=full.transactions[:-1])           # the newest row dropped, closing balance still printed
    typed_from_what_was_read = form_from(cut)
    out = transaction_matching.run(evidence(statement=cut, entered=typed_from_what_was_read))
    assert out.status is REVIEW and all(c.match is None for c in out.comparisons)
    out = transaction_matching.run(evidence(statement=cut, entered=form_from(full)))
    assert out.status is REVIEW
    # read to the end (closing balance on the newest row): the same rows are confirmed
    assert transaction_matching.run(evidence(statement=full, entered=form_from(full))).status is PASS


def test_r3_the_carried_forward_day_before_the_period_is_not_flagged():
    """A real Canara statement prints its period from the 23rd and lists the 22nd's rows (config period_grace_days)."""
    assert statement_integrity.run(evidence(statement=statement(period_start=date(2026, 7, 2)))).status is PASS
    assert statement_integrity.run(evidence(statement=statement(period_start=date(2026, 7, 3)))).status is REVIEW


def test_a_statement_without_a_printed_account_type_is_still_fully_read():
    """Canara never prints the account type: the file is read in full; rule 1 alone says the type is not confirmed."""
    from app.agents.credit.bank_verification.services.statement_service import _status

    data = statement(details=BankDetails(bank_name="Canara Bank", account_number="99901020304051"))
    assert _status(data, facts()) is ExtractionStatus.COMPLETE
    data.details.account_number = None
    assert _status(data, facts()) is ExtractionStatus.PARTIAL


# ---- R4 --------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("owner, status", [(OwnershipStatus.CONFIRMED, PASS), (OwnershipStatus.NOT_CONFIRMED, FAIL),
                                           (OwnershipStatus.PENDING, REVIEW), (None, REVIEW)])
def test_r4_only_the_branch_confirmation_decides(owner, status):
    assert account_ownership.run(evidence(ownership=owner)).status is status


def test_r4_a_matching_holder_name_alone_never_passes():
    r = account_ownership.run(evidence(ownership=None))
    assert r.status is REVIEW and r.comparisons[0].match is True


# ---- R5 --------------------------------------------------------------------------------------------------------------
def test_r5_balance_arithmetic_passes_exactly():
    r = balance_validation.run(evidence())
    assert r.status is PASS and r.details["credits"] == "₹51,000.00"


def test_r5_closing_balance_off_by_a_paisa_fails():
    assert balance_validation.run(evidence(statement=statement(closing_balance="46499.51"))).status is FAIL


def test_r5_a_running_balance_break_fails_and_is_named():
    s = statement()
    rows = list(s.transactions)
    rows[1] = rows[1].model_copy(update={"balance": D("48900.00")})
    r = balance_validation.run(evidence(statement=statement(transactions=rows)))
    assert r.status is FAIL and "05-07-2026" in str(r.comparisons)


def test_r5_reversal_is_its_own_credit_row():
    rows = [tx(1, 7, "500", "DEBIT", "9500.00", 0), tx(2, 7, "500", "CREDIT", "10000.00", 1)]
    s = statement(transactions=rows, opening_balance="10000.00", closing_balance="10000.00")
    assert balance_validation.run(evidence(statement=s)).status is PASS


def test_r5_nothing_to_check_is_not_applicable_and_partial_is_review():
    bare = [t.model_copy(update={"balance": None}) for t in statement().transactions]
    assert balance_validation.run(evidence(statement=statement(transactions=bare, opening_balance=None,
                                                               closing_balance=None))).status is NA
    assert balance_validation.run(evidence(statement=statement(opening_balance=None))).status is REVIEW


# ---- R6 --------------------------------------------------------------------------------------------------------------
def test_r6_one_source_is_not_applicable_never_a_pass():
    assert source_crosscheck.run(evidence(aa=None)).status is NA


def test_r6_agrees_and_names_the_differences():
    s = statement()
    assert source_crosscheck.run(evidence(mode=Mode.AA_SUCCESS, aa=s.model_copy(deep=True))).status is PASS
    other = s.model_copy(deep=True)
    other.transactions[0] = other.transactions[0].model_copy(update={"amount": D("50500")})
    other.details.ifsc = "MRTB0DEM999"
    r = source_crosscheck.run(evidence(mode=Mode.AA_SUCCESS, aa=other))
    assert r.status is FAIL and "IFSC" in r.reason and "Transactions" in r.reason
    assert "only in AA: 01-07-2026 ₹50,500.00 credit" in str(r.comparisons)


# ---- R7 --------------------------------------------------------------------------------------------------------------
def test_r7_exact_similar_and_none():
    keys = frozenset(f"{t.date.isoformat()}|{t.amount}|{t.direction.value}" for t in statement().transactions)
    exact = PriorDocument("BV-OLD-1", "a" * 64, None, None)
    similar = PriorDocument("BV-OLD-2", "b" * 64, "f" * 64, "99901020304051", keys)
    assert duplicate_detection.run(evidence(prior_documents=[exact])).details["match"] == "EXACT"
    r = duplicate_detection.run(evidence(prior_documents=[similar]))
    assert r.status is REVIEW and r.details["match"] == "SIMILAR" and "fraud" not in r.reason.lower()
    assert duplicate_detection.run(evidence(prior_documents=[])).status is PASS


def test_r7_the_same_file_in_the_same_case_is_not_a_duplicate():
    assert duplicate_detection.run(evidence(prior_documents=[PriorDocument("BV-T-1", "a" * 64, None, None)])).status \
        is PASS


def test_r7_no_file_is_not_applicable():
    assert duplicate_detection.run(evidence(facts=None)).status is NA


# ---- R8 --------------------------------------------------------------------------------------------------------------
def test_r8_not_applicable_never_counts_as_a_pass():
    results = run_all(evidence(ownership=OwnershipStatus.CONFIRMED))
    r8 = results[-1]
    assert r8.status is PASS and r8.details["checks_applicable"] == 6 and "R6" in r8.details["not_applicable"]
    assert r8.recommended_action == "READY_FOR_FINALIZATION"


def test_r8_failure_wins_and_action_is_deterministic():
    s = statement()
    results = run_all(evidence(statement=s, entered=form_from(s, account_type="Current")))
    assert results[-1].status is FAIL and results[-1].recommended_action == "CORRECT_OR_EXPLAIN_MISMATCH"
    assert any("Account type" in m for m in results[-1].details["mismatches"])


def test_r8_negative_ownership_escalates():
    assert run_all(evidence(ownership=OwnershipStatus.NOT_CONFIRMED))[-1].recommended_action == \
        "ESCALATE_NEGATIVE_EVIDENCE"


def test_r8_everything_not_applicable_is_not_applicable():
    from app.agents.credit.bank_verification.rules.base import result

    assert risk_assessment.run([result("R6", NA, "n/a")]).status is NA


@pytest.mark.parametrize("page, holder", [
    ("Account Statement\n01 Jul 2026 - 30 Sep 2026\nTest Person\nCRN XXXX1234\n12 Some Road\n", "Test Person"),
    ("Account Statement\nAccount Holder Name : Test Person\nCRN XXXX1234\n", "Test Person"),        # a label wins
    ("Account Statement\nBranch Address\nCIF 99887766\n", None),                                     # a label, not a name
    ("Account Statement\n12 Some Road 400001\nCRN XXXX1234\n", None),                                # digits: an address
])
def test_the_holder_printed_without_a_label_above_the_customer_id_is_read(page, holder):
    """A real Kotak statement prints the name alone above "CRN" (config statement_holder); never a label or address."""
    from app.agents.credit.bank_verification.services.statement_service import _header

    assert _header(page, page)[0].account_holder == holder


@pytest.mark.parametrize("printed, shown", [
    ("Account No CIF No \uff1a12345 \uff1a67890 : SB GEN DMND", "Savings"),   # a scan: labels on one line, values next
    ("SB-REGULAR", "Savings"), ("CURRENT ACCOUNT - BUSINESS", "Current"), ("Savings", "Savings"),
    ("Regular", None), (None, None),                                         # not an account type: never guessed
])
def test_the_account_type_is_the_forms_word_never_a_whole_header_line(printed, shown):
    """A real scan filled the type with a whole header line (owner 2026-10-27); now the type's own word, or nothing."""
    from app.agents.credit.bank_verification.services.statement_service import _account_type

    assert _account_type(printed) == shown


def test_a_full_width_colon_separates_a_label_from_its_value():
    from app.agents.credit.bank_verification.services.statement_service import _header

    page = "Account Statement\nAccount Type \uff1a SB GENERAL\n"
    assert _header(page, page)[0].account_type == "Savings"


def test_r2_a_newest_row_that_could_not_be_read_means_the_latest_three_are_not_certain():
    """A real scan left out its newest row (no readable amount) while the form was filled with older rows (owner
    2026-10-27): the latest three cannot be confirmed. An unread OLDER row changes nothing."""
    s = statement()
    newer = evidence(statement=s, facts=facts(unread_rows=[date(2026, 7, 25)]))
    out = transaction_matching.run(newer)
    assert out.status is REVIEW and "recent" in out.reason and all(c.match is None for c in out.comparisons)
    older = evidence(statement=s, facts=facts(unread_rows=[date(2026, 7, 2)]))
    assert transaction_matching.run(older).status is PASS
