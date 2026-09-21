"""
Every tool is declared, scoped, and says what it takes.

THE FAILURE THIS SUITE MAKES LOUD. A tool added to the registry without a
scope is callable by anyone holding any token, and nothing breaks when it
happens -- the planner just runs it and the response looks fine. The gap is
invisible in review because the registry is a dict of names to functions and
the permission table is keyed by intent, somewhere else.

So the contracts are checked AGAINST THE CODE rather than read alongside it:
every registered tool must have one, every contract's schema must match the
function's real signature, every scope key must resolve through the shipped
configuration, and every write must be marked as one. A declaration that
drifts from the implementation fails here instead of in production.
"""

from __future__ import annotations

import inspect

import pytest

from app.mcp import contracts
from app.mcp.applicant import ALL_TOOLS, READ_TOOLS, WRITE_TOOLS

TOOL_NAMES = sorted(ALL_TOOLS)


# ==========================================================================
# A. NOTHING IS UNDECLARED
# ==========================================================================


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_registered_tool_has_a_contract(name):
    assert contracts.contract_for(name) is not None, (
        f"{name} is callable and undeclared"
    )


def test_no_contract_describes_a_tool_that_does_not_exist():
    """A contract for a removed tool is documentation that lies."""
    assert set(contracts.CONTRACTS) <= set(ALL_TOOLS), (
        f"orphaned: {set(contracts.CONTRACTS) - set(ALL_TOOLS)}"
    )


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_has_a_summary_a_person_can_read(name):
    summary = contracts.CONTRACTS[name].summary
    assert len(summary) > 20
    assert summary.endswith(".")


# ==========================================================================
# B. THE SCHEMA MATCHES THE FUNCTION
# ==========================================================================


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_the_schema_declares_no_argument_the_tool_does_not_take(name):
    """
    A schema advertising an argument the function ignores is a caller
    sending data that silently does nothing.
    """
    declared = set(contracts.CONTRACTS[name].input_schema["properties"])
    actual = set(inspect.signature(ALL_TOOLS[name]).parameters)

    assert declared <= actual, f"{name} declares unknown {declared - actual}"


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_argument_the_tool_takes_is_declared(name):
    """The other direction: an undeclared argument is one no client sends."""
    declared = set(contracts.CONTRACTS[name].input_schema["properties"])
    actual = set(inspect.signature(ALL_TOOLS[name]).parameters)

    assert actual <= declared, f"{name} does not declare {actual - declared}"


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_required_arguments_are_the_ones_with_no_default(name):
    """
    `required` has to mean "the tool refuses without it", not "callers
    usually send it".
    """
    signature = inspect.signature(ALL_TOOLS[name])
    mandatory = {
        parameter for parameter, spec in signature.parameters.items()
        if spec.default is inspect.Parameter.empty
    }
    declared = set(contracts.CONTRACTS[name].input_schema.get("required") or [])

    assert declared == mandatory, f"{name}: {declared} vs {mandatory}"


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_no_schema_accepts_unknown_arguments(name):
    """
    Accepting an extra argument hides a caller's typo until the day the
    argument matters.
    """
    assert contracts.CONTRACTS[name].input_schema["additionalProperties"] is False


# ==========================================================================
# C. SCOPES ARE REAL AND RESOLVE
# ==========================================================================


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_resolves_to_a_configured_scope(name):
    """
    THE ONE THAT MATTERS. A scope key with no entry in the shipped
    configuration resolves to None, the check passes vacuously, and the
    tool is open to any token.
    """
    scope = contracts.required_scope(name)
    assert scope, f"{name} resolves to no scope"


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_a_write_tool_takes_its_scope_from_the_write_table(name):
    """
    A write whose scope came from the read table would be satisfied by the
    read-all scope, which is granted to service accounts precisely because
    it grants no write.
    """
    from app.agents.applicant import config

    contract = contracts.CONTRACTS[name]
    assert contract.writes is True
    assert contract.scope_key in config.write_scopes()


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
def test_a_read_tool_is_not_marked_as_a_write(name):
    from app.agents.applicant import config

    contract = contracts.CONTRACTS[name]
    assert contract.writes is False
    assert contract.scope_key in config.read_scopes()


def test_the_read_all_scope_is_not_claimed_by_any_tool():
    """
    It is a convenience granted at the check, not a requirement declared
    by a tool. Declaring it would make a tool callable by exactly the
    tokens meant to be read-only.
    """
    from app.agents.applicant import config

    assert config.read_all_scope() not in {
        contracts.required_scope(name) for name in TOOL_NAMES
    }


# ==========================================================================
# D. WRITES ARE CONFIRMED AND RECORDED
# ==========================================================================


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_every_write_needs_a_confirmation(name):
    """
    The agent proposes a write and hands it back for the caller to
    confirm. A write that could be applied straight from a chat message
    is a write a prompt can cause.
    """
    assert contracts.CONTRACTS[name].needs_confirmation is True


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_call_is_recorded(name):
    assert contracts.CONTRACTS[name].audited is True


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
def test_a_read_does_not_ask_for_a_confirmation(name):
    """Confirming a read trains an officer to click through confirmations."""
    assert contracts.CONTRACTS[name].needs_confirmation is False


# ==========================================================================
# E. THE STAGE BOUNDARY IS DECLARED, NOT ONLY OBSERVED
# ==========================================================================


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
def test_every_read_tool_names_what_it_must_never_answer(name):
    """
    The FOS stage does not own credit, risk, KYC, fraud or transaction
    analysis. Writing that on each read tool turns a convention into
    something a reviewer extending the tool has to actively delete.
    """
    never = contracts.CONTRACTS[name].never
    assert never
    assert set(never) == set(contracts.DOWNSTREAM_CONCERNS)


def test_the_denied_capabilities_in_configuration_match_the_declared_ones():
    """
    Two lists of what this stage refuses is one list too many. They are
    checked against each other rather than kept in step by hand.
    """
    from app.agents.applicant import config

    declared = " ".join(contracts.DOWNSTREAM_CONCERNS).lower()
    for capability in config.denied_capabilities():
        head = str(capability).split("_")[0].lower()
        assert head in declared, (
            f"configuration denies {capability}, which no tool contract "
            f"mentions"
        )


# ==========================================================================
# F. THE PUBLISHED CATALOGUE
# ==========================================================================


def test_the_catalogue_covers_every_tool():
    published = {entry["name"] for entry in contracts.catalogue()}
    assert published == set(ALL_TOOLS)


def test_the_catalogue_marks_reads_as_read_only():
    by_name = {e["name"]: e for e in contracts.catalogue()}

    for name in READ_TOOLS:
        assert by_name[name]["annotations"]["readOnlyHint"] is True
    for name in WRITE_TOOLS:
        assert by_name[name]["annotations"]["readOnlyHint"] is False


def test_the_catalogue_carries_a_usable_schema_for_every_tool():
    for entry in contracts.catalogue():
        schema = entry["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema
        assert entry["description"]


def test_nothing_in_the_catalogue_is_marked_destructive():
    """
    Nothing here deletes. A re-upload request records a decision; it does
    not remove the verification verdict, and a client should not warn as
    though it did.
    """
    assert all(not e["annotations"]["destructiveHint"]
               for e in contracts.catalogue())
