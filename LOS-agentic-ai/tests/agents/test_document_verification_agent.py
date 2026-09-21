"""
Document Verification Agent wiring.

This agent was dead for as long as it existed: its prompts module held a copy
of the Fraud & Risk prompts instead of the instructions it imports, so it
raised ImportError before reaching a single line of its own logic. Nothing
caught that, because every caller of the agent wraps it in a fallback.

These tests are cheap and import-level on purpose -- they would have caught it.
"""

from __future__ import annotations

import pytest


def test_instructions_exist_and_are_importable():
    from app.agents.document_verification.prompts import (
        DOCUMENT_AGENT_INSTRUCTIONS,
    )

    assert isinstance(DOCUMENT_AGENT_INSTRUCTIONS, str)
    assert DOCUMENT_AGENT_INSTRUCTIONS.strip()


def test_instructions_are_for_this_agent_not_a_copy_of_another():
    """The exact failure: fraud-risk prompt text living in this package."""
    from app.agents.document_verification.prompts import (
        DOCUMENT_AGENT_INSTRUCTIONS,
    )

    text = DOCUMENT_AGENT_INSTRUCTIONS.lower()
    assert "verify_document" in text
    assert "document verification agent" in text
    assert "fraud & risk" not in text
    assert "risk score" not in text


def test_instructions_forbid_the_model_deciding():
    """
    The agent is a tool-using model, so the instruction text is a safety
    boundary: verification is the tool's job, never the model's.
    """
    from app.agents.document_verification.prompts import (
        DOCUMENT_AGENT_INSTRUCTIONS,
    )

    text = DOCUMENT_AGENT_INSTRUCTIONS.lower()
    assert "do not decide" in text or "not decide anything" in text
    assert "never state a verdict the tool did not return" in text
    assert "never claim a document is genuine" in text


def test_agent_builds():
    """Constructing the agent must not raise."""
    pytest.importorskip("agent_framework")

    from app.agents.document_verification.agent import build_document_agent

    agent, mcp_server = build_document_agent()

    assert agent.name == "document_verification_agent"
    assert mcp_server.name == "document_verification_mcp"
    assert set(mcp_server.allowed_tools) == {
        "verify_document", "list_enabled_document_types",
    }


def test_the_registry_compatibility_entry_point_works():
    """registry.get_document_agent() was unreachable while the import failed."""
    pytest.importorskip("agent_framework")

    import app.orchestration.registry as registry

    agent, _mcp = registry.get_document_agent()
    assert agent.name == "document_verification_agent"


def test_disabled_agent_refuses_to_build(monkeypatch):
    monkeypatch.setenv("DOCUMENT_AGENT_ENABLED", "false")

    from app.agents.document_verification.agent import build_document_agent

    with pytest.raises(RuntimeError):
        build_document_agent()


def test_fraud_risk_prompts_are_unaffected():
    """
    The fraud-risk prompts live in their own module and always did. Removing
    the stray copy must not have touched them.
    """
    from app.agents.fraud_risk.prompts import PROMPT_VERSIONS, get_system_prompt

    assert set(PROMPT_VERSIONS) == {"v1", "v2"}
    assert "Fraud & Risk Agent" in get_system_prompt("v1")


def test_the_ollama_factory_the_agent_depends_on_constructs():
    """
    The agent's other dependency. agent_framework renamed the constructor's
    model argument, so this factory raised TypeError for every caller.
    """
    pytest.importorskip("agent_framework")

    from app.llm.provider import create_ollama_client

    client = create_ollama_client()
    assert client.model
    assert client.host
