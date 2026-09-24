"""
`account_holder_name`, and why it is only ever a second name for `name`.

WHY THE ALIAS EXISTS. On a bank statement, `name` IS the account
holder. It is called `name` because that is the key cross-document
identity reads -- `mapping._NAME_FIELDS` -- so one field serves both
the API and KYC without anything bank-specific in the identity code.
But somebody reading the response looks for "account_holder", does not
find it, and concludes the holder was never extracted. That happened
in review, twice.

WHY IT IS A COPY AND NOT A SECOND EXTRACTION. Two fields that are
separately derived are two fields that can disagree, and the first
time they did, one of them would be quietly wrong in an identity
check. This one is assigned from `name` at the moment of publication,
so disagreement is not possible.

WHAT IT DOES NOT DO. It does not appear when the holder is unknown; it
is not derived, inferred or defaulted; and nothing reads it back.
Removing it would change no behaviour anywhere in the service.
"""

from __future__ import annotations

import pathlib

import pytest

from app.agents.document_agent.workflow import _serialise_financial_extraction
from app.agents.financial.agent import _from_bank_statement
from app.agents.financial.schemas import (
    FinancialDocumentType,
    FinancialResult,
    FinancialStatus,
)

LABELLED = pathlib.Path("samples/documents/Canara Bank Statement.pdf")
UNLABELLED = pathlib.Path("samples/real_batch/bank_kotak.pdf")


def fields_of(result: FinancialResult) -> dict:
    return _serialise_financial_extraction(
        result, include_detail=False).get("fields", {})


def statement(name: str | None) -> FinancialResult:
    return FinancialResult(
        document_type=FinancialDocumentType.BANK_STATEMENT,
        status=FinancialStatus.SUCCESS,
        name=name,
    )


# ==========================================================================
# A. THE TWO FIELDS ARE ONE VALUE
# ==========================================================================


def test_the_alias_carries_the_same_value():
    fields = fields_of(statement("GUDDI DEVI"))

    assert fields["name"] == "GUDDI DEVI"
    assert fields["account_holder_name"] == fields["name"]


def test_the_original_field_is_still_published():
    """
    KYC reads `name`. Replacing it rather than aliasing it would take
    the account holder out of cross-document identity entirely.
    """
    assert "name" in fields_of(statement("A PERSON"))


def test_an_unknown_holder_produces_neither_field():
    """
    NOT INVENTED, NOT DEFAULTED, NOT EMPTY-STRINGED. A statement that
    does not label its holder publishes no holder.
    """
    fields = fields_of(statement(None))

    assert "name" not in fields
    assert "account_holder_name" not in fields


@pytest.mark.parametrize("name", ["GUDDI DEVI", "A B", None, ""])
def test_the_two_fields_never_disagree(name):
    """
    THE INVARIANT, stated directly. Whatever `name` ends up being --
    published, absent, or an oddity the parser never emits -- the
    alias is that and nothing else. Two independently derived fields
    could disagree about whose account this is; a copy cannot.
    """
    fields = fields_of(statement(name))

    assert fields.get("account_holder_name") == fields.get("name")


# ==========================================================================
# B. ONLY ON A BANK STATEMENT
# ==========================================================================


def test_a_salary_slip_name_is_not_an_account_holder():
    """
    `name` on a salary slip is the employee, which is not the holder
    of any account. The alias says what it means or it is not
    published at all.
    """
    slip = FinancialResult(
        document_type=FinancialDocumentType.SALARY_SLIP,
        status=FinancialStatus.SUCCESS,
        name="AN EMPLOYEE",
    )

    fields = fields_of(slip)

    assert fields["name"] == "AN EMPLOYEE"
    assert "account_holder_name" not in fields


# ==========================================================================
# C. THROUGH THE REAL PARSER
# ==========================================================================


@pytest.mark.skipif(not LABELLED.exists(), reason="sample not available")
def test_a_labelled_statement_publishes_both():
    fields = fields_of(_from_bank_statement(str(LABELLED)))

    assert fields["account_holder_name"] == fields["name"]
    assert fields["account_holder_name"]


@pytest.mark.skipif(not UNLABELLED.exists(), reason="sample not available")
def test_an_unlabelled_statement_publishes_neither():
    """
    This statement's holder is the second line of the page, with no
    caption and no honorific. The parser refuses to guess, and the
    alias has nothing to copy.
    """
    fields = fields_of(_from_bank_statement(str(UNLABELLED)))

    assert "account_holder_name" not in fields
    assert "name" not in fields


# ==========================================================================
# D. NOTHING READS IT BACK
# ==========================================================================


def test_identity_still_reads_the_original_field_only():
    """
    The alias is for a reader of the response. If KYC ever started
    reading it, the two fields would become two sources and could
    disagree about whose account this is.
    """
    from app.agents.los import mapping

    assert "account_holder_name" not in mapping._NAME_FIELDS
    assert "name" in mapping._NAME_FIELDS
