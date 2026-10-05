"""
THE LOAN AGENT'S VOICE (production default): the customer is spoken ABOUT, not TO.
The customer channel (APPLICANT_AGENT_AUDIENCE=customer) is unchanged.
"""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.answering import voice


@pytest.mark.parametrize("customer, agent", [
    ("Your PAN number is XXXXXX189E.", "The customer's PAN number is XXXXXX189E."),
    ("You haven't provided your address yet.", "The customer hasn't provided their address yet."),
    ("Your next step is to capture the address.", "The next step is to capture the address."),
    ("Your application is under review.", "The application is under review."),
    ("Your KYC check needs review.", "The customer's KYC check needs review."),
    ("Aapka naam Laxmi record hai.", "Customer ka naam Laxmi record hai."),
    ("आपका PAN सत्यापित है।", "ग्राहक का PAN सत्यापित है।"),
    ("Upload your bank statement here.", "Upload the customer's bank statement here."),
])
def test_the_agent_hears_about_the_customer(customer, agent):
    assert voice.for_audience(customer, "agent") == agent


@pytest.mark.parametrize("text", [
    "I can help you with the application.",          # addressed to the agent
    "Aapka swagat hai.",                              # a greeting to the agent
    "The co-applicant's KYC check needs review.",    # already third person
    "PAN number XXXXXX189E, score 100.",              # values untouched
])
def test_what_is_addressed_to_the_agent_or_already_third_person_is_untouched(text):
    assert voice.for_audience(text, "agent") == text


def test_the_customer_channel_is_unchanged():
    assert voice.for_audience("Your PAN number is XXXXXX189E.", "customer") == "Your PAN number is XXXXXX189E."


def test_production_defaults_to_the_agent(monkeypatch):
    monkeypatch.delenv("APPLICANT_AGENT_AUDIENCE", raising=False)
    assert voice.audience() == "agent"
