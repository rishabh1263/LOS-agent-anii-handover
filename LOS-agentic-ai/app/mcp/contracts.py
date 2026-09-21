"""
The tool contracts, declared once and in one place.

WHAT WAS MISSING. `ALL_TOOLS` maps a capability name to a coroutine, which is
everything the planner needs and nothing a reviewer does. Reading it tells
you a tool exists; it does not tell you what arguments it takes, what scope
calling it requires, whether it writes, or whether the call is recorded.
Those four facts were each true somewhere -- in a function signature, in a
permissions table keyed by INTENT rather than by tool, in whether the author
remembered to pass `write=True` to the audit call -- and nowhere together.

That is a security surface. A tool added without a scope is a tool anyone
with any token can call, and nothing fails when it happens: the planner just
runs it. The contracts here make that omission a test failure instead.

WHAT A CONTRACT IS NOT. It is not a second permission check. Authorisation
still happens in app/agents/applicant/permissions.py, against the intent,
before any tool runs. This DECLARES the requirement so it can be audited,
published and tested against the enforcement -- a declaration that disagreed
with the enforcement would be caught by the tests in
tests/agents/test_mcp_contracts.py.

THE SCHEMAS ARE JSON SCHEMA because that is what an MCP client expects, and
because a hand-written prose description of an argument list drifts from the
signature within two changes. A test checks each schema against the actual
function signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ==========================================================================
# WHAT A CONTRACT SAYS
# ==========================================================================


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


CASE_ID = _string("The case this applies to.")
APPLICANT_ID = _string("The applicant this applies to.")


@dataclass(frozen=True)
class ToolContract:
    """One tool, completely described."""

    name: str
    summary: str
    #: JSON Schema for the arguments. `required` is what the tool will
    #: refuse without, not what a caller usually sends.
    input_schema: dict[str, Any]
    #: The configuration KEY naming the scope, resolved through
    #: applicant_agent.yaml rather than hardcoded -- a deployment that
    #: renames its scopes must not have to edit Python.
    scope_key: str
    #: Whether calling it changes stored data.
    writes: bool = False
    #: Whether a call is recorded. Every one is; the field exists so that
    #: switching one off is a visible change rather than a missing line.
    audited: bool = True
    #: Whether a write needs the caller to confirm before it is applied.
    #: Reads leave it false.
    needs_confirmation: bool = False
    #: Downstream concerns this tool must never be extended to answer.
    #: Documentation with teeth: a test reads it.
    never: tuple[str, ...] = field(default_factory=tuple)

    def public(self) -> dict[str, Any]:
        """The description an MCP client is given."""
        return {
            "name": self.name,
            "description": self.summary,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": not self.writes,
                "destructiveHint": False,
                "requiresConfirmation": self.needs_confirmation,
            },
        }


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        # A tool that silently accepts an unknown argument hides a typo in a
        # caller until the day the argument matters.
        "additionalProperties": False,
    }


#: The concerns this stage does not own, stated once.
#:
#: Each of these corresponds to an entry in the `permissions.denied` list in
#: applicant_agent.yaml, and a test checks the two against each other. RCU
#: is named separately from fraud even though the unit investigates fraud:
#: the configuration denies `rcu_analysis` by that name, and a list that
#: covered it only in spirit would let a rename go unnoticed.
DOWNSTREAM_CONCERNS = (
    "credit assessment", "risk scoring", "KYC decisioning",
    "RCU and fraud investigation", "financial analysis of transactions",
)


# ==========================================================================
# THE CONTRACTS
# ==========================================================================

CONTRACTS: dict[str, ToolContract] = {
    # ---- reads ----------------------------------------------------------
    "applicant.get": ToolContract(
        name="applicant.get",
        summary="The stored applicant record.",
        input_schema=_schema({"applicant_id": APPLICANT_ID}, ["applicant_id"]),
        scope_key="applicant",
        never=DOWNSTREAM_CONCERNS,
    ),
    "application.get": ToolContract(
        name="application.get",
        summary="The stored application record for one case.",
        input_schema=_schema({"case_id": CASE_ID}, ["case_id"]),
        scope_key="application",
        never=DOWNSTREAM_CONCERNS,
    ),
    "applications.list": ToolContract(
        name="applications.list",
        summary="Every application belonging to one applicant.",
        input_schema=_schema({"applicant_id": APPLICANT_ID}, ["applicant_id"]),
        scope_key="application",
        never=DOWNSTREAM_CONCERNS,
    ),
    "documents.get": ToolContract(
        name="documents.get",
        summary="The documents attached to a case, with their stored status.",
        input_schema=_schema({"case_id": CASE_ID}, ["case_id"]),
        scope_key="documents",
        never=DOWNSTREAM_CONCERNS,
    ),
    "documents.checklist": ToolContract(
        name="documents.checklist",
        summary=(
            "The required-document checklist for a case, resolved from the "
            "configured policy, measured against what is stored, and "
            "carrying the policy provenance behind every row."
        ),
        input_schema=_schema({"case_id": CASE_ID}, ["case_id"]),
        scope_key="documents",
        never=DOWNSTREAM_CONCERNS,
    ),
    "documents.verification": ToolContract(
        name="documents.verification",
        summary=(
            "What verification concluded about one document type. Reports "
            "the pipeline's verdict; it does not recompute one."
        ),
        input_schema=_schema(
            {"case_id": CASE_ID,
             "document_type": _string("The document type to report on.")},
            ["case_id", "document_type"],
        ),
        scope_key="verification",
        never=DOWNSTREAM_CONCERNS,
    ),
    "workflow.pending_items": ToolContract(
        name="workflow.pending_items",
        summary="Everything standing between this case and the CPA handoff.",
        input_schema=_schema({"case_id": CASE_ID}, ["case_id"]),
        scope_key="pending_items",
        never=DOWNSTREAM_CONCERNS,
    ),
    "workflow.next_action": ToolContract(
        name="workflow.next_action",
        summary="The single next thing the field officer should do.",
        input_schema=_schema({"case_id": CASE_ID}, ["case_id"]),
        scope_key="next_action",
        never=DOWNSTREAM_CONCERNS,
    ),
    "workflow.readiness": ToolContract(
        name="workflow.readiness",
        summary=(
            "Whether the case may be handed to CPA, and what is blocking it "
            "if not. A FOS-stage completeness gate, not a credit decision."
        ),
        input_schema=_schema({"case_id": CASE_ID}, ["case_id"]),
        scope_key="next_action",
        never=DOWNSTREAM_CONCERNS,
    ),
    "applicant.360": ToolContract(
        name="applicant.360",
        summary=(
            "The whole FOS-stage picture for one case in one call. Composes "
            "the reads above; computes nothing of its own."
        ),
        input_schema=_schema({"case_id": CASE_ID}, ["case_id"]),
        # The broadest read it composes.
        scope_key="applicant",
        never=DOWNSTREAM_CONCERNS,
    ),

    # ---- writes ---------------------------------------------------------
    #
    # EVERY ONE NEEDS CONFIRMATION. The agent proposes a write and returns
    # it as an action for the caller to confirm; it does not apply one from
    # a chat message. A model that could write without a confirmation step
    # is a model a prompt can make write.
    "applicant.create": ToolContract(
        name="applicant.create",
        summary="Create an applicant record.",
        input_schema=_schema(
            {
                "full_name": _string("The applicant's name."),
                "mobile": _string("Contact number."),
                "email": _string("Email address."),
                "date_of_birth": _string("Date of birth, ISO format."),
                "address": _string("Postal address."),
                "applicant_id": _string("Optional. Generated when omitted."),
            },
            [],
        ),
        scope_key="create_applicant",
        writes=True,
        needs_confirmation=True,
    ),
    "applicant.update": ToolContract(
        name="applicant.update",
        summary="Update fields on an existing applicant record.",
        input_schema=_schema(
            {
                "applicant_id": APPLICANT_ID,
                "full_name": _string("The applicant's name."),
                "mobile": _string("Contact number."),
                "email": _string("Email address."),
                "date_of_birth": _string("Date of birth, ISO format."),
                "address": _string("Postal address."),
            },
            ["applicant_id"],
        ),
        scope_key="update_applicant",
        writes=True,
        needs_confirmation=True,
    ),
    "application.create": ToolContract(
        name="application.create",
        summary=(
            "Open an application for an existing applicant, and pin the "
            "document policy version it is assessed under."
        ),
        input_schema=_schema(
            {
                "applicant_id": APPLICANT_ID,
                "product": _string("Decides the document checklist."),
                "loan_amount": _string(
                    "Drives amount-based document rules. Omitted means "
                    "those rules are reported as unevaluated, not guessed."),
                "employment_type": _string(
                    "An attribute the document policy may key on. Not "
                    "defaulted."),
                "case_id": _string("Optional. Generated when omitted."),
            },
            ["applicant_id"],
        ),
        scope_key="create_application",
        writes=True,
        needs_confirmation=True,
    ),
    "application.update": ToolContract(
        name="application.update",
        summary="Update the product, amount or attributes of an application.",
        input_schema=_schema(
            {
                "case_id": CASE_ID,
                "product": _string("Decides the document checklist."),
                "loan_amount": _string("Drives amount-based document rules."),
                "employment_type": _string(
                    "An attribute the document policy may key on."),
            },
            ["case_id"],
        ),
        scope_key="create_application",
        writes=True,
        needs_confirmation=True,
    ),
    "documents.mark_for_reupload": ToolContract(
        name="documents.mark_for_reupload",
        summary=(
            "Record the officer's decision that a document must be "
            "collected again. Does NOT change what verification concluded."
        ),
        input_schema=_schema(
            {"case_id": CASE_ID,
             "document_type": _string("The document to collect again.")},
            ["case_id", "document_type"],
        ),
        scope_key="upload_document",
        writes=True,
        needs_confirmation=True,
    ),
}


def contract_for(name: str) -> ToolContract | None:
    return CONTRACTS.get(name)


def catalogue() -> list[dict[str, Any]]:
    """Every tool, as an MCP client would be told about it."""
    return [CONTRACTS[name].public() for name in sorted(CONTRACTS)]


def required_scope(name: str) -> str | None:
    """
    The resolved scope string for a tool, through configuration.

    None means the tool is not under a scope, which for a tool in this
    registry is a configuration error rather than an intent -- the test
    suite treats it as one.
    """
    from app.agents.applicant import config

    contract = CONTRACTS.get(name)
    if contract is None:
        return None
    table = config.write_scopes() if contract.writes else config.read_scopes()
    return table.get(contract.scope_key)


__all__ = ["CONTRACTS", "DOWNSTREAM_CONCERNS", "ToolContract", "catalogue",
           "contract_for", "required_scope"]
