"""
THE CAPABILITY REGISTRY -- what the Copilot can answer, declared once.

Each capability names the SEMANTIC OBJECT it is about, the fields it can
report, the authoritative source (the MCP tools that read the records), the
scope a caller needs, the fields that may appear in an answer, and whether a
conversation referent ("this case", "that document") may be inherited.

DATA, NOT RULES. Nothing here matches a sentence: the semantic frame and the
field vocabulary (profile.py, semantic_concepts.yaml) decide what a question
means; this registry says what an answer to it may contain and where the
value comes from. A field that is not listed as an output field is never
published, whatever a record carries.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    name: str
    object: str
    source: tuple[str, ...]                       # MCP tools, the authoritative read
    scope: str = "fos.read"
    fields: tuple[str, ...] = ()
    output_fields: tuple[str, ...] = ()
    inherits_referents: bool = True
    intent: str | None = None                     # the agent intent that serves it


#: Public, non-secret fields of the applicant record (applicant.get).
APPLICANT_FIELDS = ("full_name", "mobile", "email", "date_of_birth", "address")
#: Public fields of the application record (application.get). Never the
#: internal row id, never audit or security metadata.
APPLICATION_FIELDS = ("case_id", "applicant_id", "product", "loan_amount", "employment_type",
                      "tenure_months", "interest_rate_pct", "declared_monthly_income",
                      "declared_monthly_obligations", "property_value")
FINANCIAL_FIELDS = ("loan_amount", "product", "tenure_months", "interest_rate_pct",
                    "declared_monthly_income", "declared_monthly_obligations", "property_value")
IDENTIFIER_FIELDS = ("case_id", "applicant_id")

REGISTRY: dict[str, Capability] = {c.name: c for c in (
    Capability("APPLICANT_PROFILE", "APPLICANT", ("applicant.get",), fields=APPLICANT_FIELDS,
               output_fields=APPLICANT_FIELDS, intent="APPLICANT_PROFILE"),
    Capability("APPLICATION_METADATA", "APPLICATION", ("application.get",),
               fields=APPLICATION_FIELDS, output_fields=APPLICATION_FIELDS,
               intent="APPLICANT_PROFILE"),
    Capability("CASE_IDENTIFIER", "APPLICATION", ("application.get",), fields=IDENTIFIER_FIELDS,
               output_fields=IDENTIFIER_FIELDS, intent="APPLICANT_PROFILE"),
    Capability("APPLICATION_STATUS", "APPLICATION", ("application.get",), intent="APPLICATION_STATUS"),
    Capability("CURRENT_STAGE", "STAGE", ("applicant.360",), intent="APPLICATION_STAGE"),
    Capability("CASE_HISTORY", "CASE", ("case.memory",), intent="CASE_HISTORY",
               output_fields=("finding_kind", "status", "reason_codes", "comparisons")),
    Capability("CASE_FINDINGS", "CASE", ("case.memory",), intent="CASE_FINDINGS",
               output_fields=("finding_kind", "status", "reason_codes", "score")),
    Capability("CASE_REASON_CODES", "CASE", ("case.memory",), intent="CASE_FINDINGS",
               output_fields=("reason_codes",)),
    Capability("DOCUMENTS", "DOCUMENTS", ("documents.get",), intent="DOCUMENTS_UPLOADED"),
    Capability("DOCUMENT_REQUIREMENTS", "DOCUMENTS", ("documents.checklist",),
               intent="DOCUMENTS_REQUIRED"),
    Capability("DOCUMENT_PENDING", "DOCUMENTS", ("documents.checklist",), intent="DOCUMENTS_PENDING"),
    Capability("DOCUMENT_VERIFICATION", "DOCUMENTS", ("documents.get",),
               intent="DOCUMENT_VERIFICATION"),
    Capability("DOCUMENT_CONTENT", "DOCUMENT", ("documents.get",), intent="DOCUMENT_DETAILS"),
    Capability("KYC_RESULT", "CASE", ("case.memory",), intent="KYC_RESULT",
               fields=("status", "score"), output_fields=("status", "score", "reason_codes")),
    Capability("KYC_SCORE", "CASE", ("case.memory",), intent="KYC_RESULT", fields=("score",),
               output_fields=("score",)),
    Capability("KYC_FIELDS", "CASE", ("case.memory",), intent="KYC_RESULT",
               output_fields=("comparisons",)),
    Capability("NEXT_ACTION", "CASE", ("workflow.next_action",), intent="NEXT_ACTION"),
    Capability("READINESS", "CASE", ("workflow.readiness",), intent="READINESS"),
    Capability("KNOWLEDGE", "PROCESS_TERM", (), intent="FOS_KNOWLEDGE", inherits_referents=False),
    Capability("GENERAL_CONVERSATION", "NONE", (), intent="CONVERSATION", inherits_referents=False),
)}


def names() -> list[str]:
    return list(REGISTRY)


__all__ = ["APPLICANT_FIELDS", "APPLICATION_FIELDS", "Capability", "FINANCIAL_FIELDS",
           "IDENTIFIER_FIELDS", "REGISTRY", "names"]
