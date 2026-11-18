"""
The bank verification agent's data: what each source said (AA, the statement, the CPA's form, the agency), what each
rule found, and what the agency decided. Money is Decimal end to end; an account number leaves the API masked.
"""

from __future__ import annotations

from datetime import date as Date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Mode(str, Enum):
    AA_SUCCESS = "AA_SUCCESS"        # the Account Aggregator returned data and the statement
    AA_FAILURE = "AA_FAILURE"        # no AA data: the CPA uploads the statement / enters the details


class Direction(str, Enum):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


class Provenance(str, Enum):
    AA = "AA"                        # from the (simulated) Account Aggregator
    EXTRACTED = "EXTRACTED"          # read off the statement by the agent
    MANUAL = "MANUAL"                # typed by the CPA user
    AGENCY = "AGENCY"                # confirmed by the verification agency
    APPLICATION = "APPLICATION"      # the bank account the customer gave on the loan application
    CONFIRMED = "CONFIRMED"          # read off the statement, then checked against it by the CPA


class RuleStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class OwnershipStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    PENDING = "PENDING"


class ExtractionStatus(str, Enum):
    COMPLETE = "COMPLETE"            # header fields and transactions read
    PARTIAL = "PARTIAL"              # some of it read; the rest needs manual entry
    UNREADABLE = "UNREADABLE"        # no text layer (scan) / protected / damaged: manual entry
    INVALID = "INVALID"              # not a statement file at all


class FinalStatus(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"   # non-final: the case stays open


class ChecklistAnswer(str, Enum):
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    NOT_VERIFIED = "NOT_VERIFIED"


class BranchVisit(str, Enum):
    COMPLETED = "COMPLETED"
    SCHEDULED = "SCHEDULED"
    NOT_DONE = "NOT_DONE"


def money(value: Any) -> Decimal | None:
    """An amount as an exact Decimal (commas and the rupee sign allowed); None for blank. Never a float."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, float):
        value = repr(value)
    text = str(value).replace(",", "").replace("₹", "").replace("Rs.", "").replace("INR", "").strip()
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"not an amount: {value!r}")
    return amount.quantize(Decimal("0.01"))


def rupees(amount: Decimal | str | None) -> str | None:
    """An amount the Indian way: 125979.25 -> '₹1,25,979.25' (None stays None)."""
    if amount is None:
        return None
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    sign, whole, paise = "-" if value < 0 else "", f"{abs(value):.2f}".split(".")[0], f"{abs(value):.2f}".split(".")[1]
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    grouped = ",".join(([head] if head else []) + groups + [tail]) if head or groups else tail
    return f"{sign}₹{grouped}.{paise}"


def mask(account_number: str | None) -> str | None:
    """Every digit but the last four hidden; None stays None."""
    if not account_number:
        return None
    raw = str(account_number).strip()
    return "X" * max(len(raw) - 4, 0) + raw[-4:]


class Transaction(BaseModel):
    """One statement / AA transaction, in the order the source printed it (`seq`)."""
    model_config = ConfigDict(frozen=True)

    date: Date
    amount: Decimal
    direction: Direction
    balance: Decimal | None = None
    narration: str = ""
    reference: str | None = None
    seq: int = 0

    @field_validator("amount", "balance", mode="before")
    @classmethod
    def _exact(cls, value: Any) -> Decimal | None:
        return money(value)

    @field_validator("amount")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if value is None or value <= 0:
            raise ValueError("a transaction amount must be greater than zero")
        return value


class BankDetails(BaseModel):
    bank_name: str | None = None
    account_number: str | None = None
    account_type: str | None = None
    ifsc: str | None = None
    account_holder: str | None = None


class StatementData(BaseModel):
    """What ONE source says about the account: the AA response, or a statement read by the agent."""
    details: BankDetails = Field(default_factory=BankDetails)
    transactions: list[Transaction] = Field(default_factory=list)
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    period_start: Date | None = None
    period_end: Date | None = None

    @field_validator("opening_balance", "closing_balance", mode="before")
    @classmethod
    def _exact(cls, value: Any) -> Decimal | None:
        return money(value)


class DocumentFacts(BaseModel):
    """What the agent found about the statement FILE (rule 3, rule 7)."""
    document_id: str | None = None
    file_name: str | None = None
    kind: str | None = None                  # PDF / CSV
    size: int = 0
    sha256: str | None = None
    fingerprint: str | None = None           # account + transactions, independent of the bytes
    pages: int = 0
    pages_with_text: int = 0
    encrypted: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)
    extraction_status: ExtractionStatus = ExtractionStatus.UNREADABLE
    notes: list[str] = Field(default_factory=list)
    seeded: bool = False                     # the case's seeded synthetic statement, not an upload
    scanned: bool = False                    # a scan read by OCR (no text layer)
    unread_rows: list[Date] = Field(default_factory=list)   # dates of rows left out (no readable amount)


class EnteredTransaction(BaseModel):
    """One of the three transaction rows on the Leviosa form; a blank row is allowed (it is reported, never guessed)."""
    date: Date | None = None
    amount: Decimal | None = None
    direction: Direction | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _exact(cls, value: Any) -> Decimal | None:
        return money(value)

    @field_validator("amount")
    @classmethod
    def _positive(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("a transaction amount must be greater than zero")
        return value

    @field_validator("date")
    @classmethod
    def _not_future(cls, value: Date | None) -> Date | None:
        from app.agents.credit.bank_verification.config import cfg

        if value is not None and value > Date.today():
            raise ValueError("a transaction date cannot be in the future")
        earliest = (cfg().get("form_suggestions") or {}).get("earliest_transaction_date")
        if value is not None and earliest and value < Date.fromisoformat(str(earliest)):
            raise ValueError(f"a transaction date cannot be before {earliest}")
        return value

    def blank(self) -> bool:
        return self.date is None and self.amount is None and self.direction is None


class EnteredDetails(BaseModel):
    """The Leviosa Bank Verification form: populated from AA, from the statement, or typed by the CPA."""
    bank_name: str | None = None
    account_number: str | None = None
    account_type: str | None = None
    transactions: list[EnteredTransaction] = Field(default_factory=list, max_length=3)
    provenance: dict[str, Provenance] = Field(default_factory=dict)

    @field_validator("account_number")
    @classmethod
    def _digits(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        raw = "".join(str(value).split()).upper()
        # digits, or digits masked with X as the AA sends them ("XXXXXX1234") -- never a name typed in the wrong box
        if not raw or len(raw) > 20 or any(ch not in "0123456789X" for ch in raw) or not any(ch.isdigit() for ch in raw):
            raise ValueError("an account number is digits only (X for masked digits), at most 20")
        return raw


class Comparison(BaseModel):
    field: str
    left_label: str
    left: str | None = None
    right_label: str
    right: str | None = None
    match: bool | None = None                # None: could not be compared
    note: str | None = None


class RiskFlag(BaseModel):
    code: str
    severity: Severity
    message: str
    rule_id: str


class RuleResult(BaseModel):
    rule_id: str
    rule_name: str
    status: RuleStatus
    reason: str
    comparisons: list[Comparison] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    recommended_action: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ProcessRequest(BaseModel):
    """API 1. `statement_source`: UPLOAD (a file in this request), ON_FILE (the case's latest statement, the
    default), SEEDED (the case's seeded synthetic statement), NONE (no statement)."""
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9-]+$")
    mode: Mode
    statement_source: str = Field(default="ON_FILE", pattern=r"^(UPLOAD|ON_FILE|SEEDED|NONE)$")
    entered: EnteredDetails | None = None
    agency_id: str | None = None


class WaiveRequest(BaseModel):
    """Waive the bank verification of a case with AA data: the reason is required."""
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=1000)


class RetriggerRequest(BaseModel):
    """Run the checks again on the case's AA data; `entered` = the details as the officer corrected them, if he did."""
    model_config = ConfigDict(extra="forbid")

    entered: EnteredDetails | None = None


class FinalizeRequest(BaseModel):
    """API 2: the agency's checklist, branch visit, ownership outcome, decision and rationale."""
    model_config = ConfigDict(extra="forbid")

    agency_id: str
    checklist: dict[str, ChecklistAnswer]
    branch_visit: BranchVisit
    ownership: OwnershipStatus
    requested_decision: FinalStatus
    rationale: str = Field(default="", max_length=2000)
    remarks: str | None = Field(default=None, max_length=2000)
    report_document_id: str | None = None
