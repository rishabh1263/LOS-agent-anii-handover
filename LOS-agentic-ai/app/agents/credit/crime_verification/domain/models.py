"""The request bodies of the two business APIs. Shapes and sizes only; the business rules are in validation.py."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["PRIMARY", "CO_APPLICANT"]


class SubjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Role
    seq: int = Field(default=0, ge=0, le=99, description="0 for the applicant; 1..N for co-applicant 1..N "
                                                         "(a co-applicant sent without one is co-applicant 1).")

    @model_validator(mode="after")
    def _co_applicant_numbered(self) -> "SubjectInput":
        if self.role == "PRIMARY":
            self.seq = 0
        elif self.seq == 0:
            self.seq = 1
        return self
    full_name: str = Field(default="", max_length=200)
    father_name: str = Field(default="", max_length=200)
    date_of_birth: str = Field(default="", max_length=20, examples=["1991-03-14"])
    permanent_address: str = Field(default="", max_length=500)
    pan: str | None = Field(default=None, max_length=20,
                            description="Omitted: the PAN on the application for this role (the screen never "
                                        "receives a full PAN). Given: it must be that same PAN.")


class InitiateRequest(BaseModel):
    """API 1. `confirmation_accepted` must be true: the screen sends it only from the confirmation popup."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    application_id: str = Field(min_length=1, max_length=40, examples=["APP-2026-001"])
    vendor: str = Field(default="CRIME_CHECK", max_length=40)
    # the limit that matters is config subjects.max_co_applicants (checked in the service); this only bounds the body
    subjects: list[SubjectInput] = Field(default_factory=list, max_length=100)
    confirmation_accepted: bool = False
    demo_scenario: str | None = Field(default=None, max_length=40, description="DEMO MODE only.")
    retrigger: bool = False
    idempotency_key: str | None = Field(default=None, max_length=80,
                                        description="Or the Idempotency-Key header.")


class FinalizeRequest(BaseModel):
    """API 2. The officer's acknowledgement; it never carries or changes a provider result."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: str = Field(max_length=40, examples=["ACCEPTED"])
    remarks: str = Field(default="", max_length=2000)


def mask_pan(pan: str | None) -> str:
    """ABCPE1234F -> XXXXXX234F: only the last four characters show."""
    value = str(pan or "")
    return ("X" * max(len(value) - 4, 0) + value[-4:]) if value else ""


class PersonIn(BaseModel):
    """One person on an application as the LOS sends it. Details may still be incomplete (the desk shows what is
    missing); the PAN -- the identity -- is required."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Role
    seq: int = Field(default=0, ge=0, le=99)
    full_name: str = Field(default="", max_length=200)
    father_name: str = Field(default="", max_length=200)
    date_of_birth: str = Field(default="", max_length=20)
    permanent_address: str = Field(default="", max_length=500)
    pan: str = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _numbered(self) -> "PersonIn":
        if self.role == "PRIMARY":
            self.seq = 0
        elif self.seq == 0:
            self.seq = 1
        return self


class ApplicationIn(BaseModel):
    """PUT /applications/{id}: the LOS pushes an application and ALL its people (the list replaces the one held)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(default="", max_length=120)
    branch: str = Field(min_length=1, max_length=20)
    product: str = Field(min_length=1, max_length=80)
    loan_amount: float = Field(ge=0, le=1e12)
    stage: str = Field(min_length=1, max_length=60)
    status: str = Field(min_length=1, max_length=60)
    people: list[PersonIn] = Field(min_length=1, max_length=100)
