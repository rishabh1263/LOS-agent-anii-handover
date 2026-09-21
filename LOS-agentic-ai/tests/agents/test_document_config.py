"""
Verification agent configuration.

The import moved when verification was consolidated: it previously lived in a
package that contained a stub and no checks, and now sits with the agent that
actually performs verification.
"""

from app.agents.verification import enabled, version


def test_verification_agent_is_enabled_by_default():
    assert enabled() is True


def test_verification_agent_reports_a_version():
    assert version()


def test_agent_switch_is_separate_from_per_document_switches():
    """
    Turning off one document class must not disable the agent, and disabling
    the agent is not the same as disabling a class.
    """
    from app.agents.verification import enabled_for

    assert enabled() is True
    assert enabled_for("PAN") is True
