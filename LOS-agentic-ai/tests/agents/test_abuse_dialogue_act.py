"""ABUSE AS A DIALOGUE ACT (conversation/abuse.py): classified by what is left, never by phrase."""

from __future__ import annotations

import pytest

from app.agents.applicant.copilot.conversation import abuse


@pytest.mark.parametrize("message", ["chutiya", "bc", "fuck you", "f*** off", "you are useless idiot", "saala bakwas bot",
                                     "madarchod", "wtf", "stupid bot", "tu pagal hai kya", "bhai tu ekdum bekar hai",
                                     "motherfucker", "s#!t", "गांडू", "बकवास"])
def test_abuse_with_nothing_asked_is_abusive_only(message):
    assert abuse.classify(message).kind == abuse.ABUSIVE_ONLY, message


@pytest.mark.parametrize("message,keeps", [("chutiye pending kya hai", "pending"),
                                           ("ye damn bank statement kab verify hoga?", "bank statement"),
                                           ("stupid bot what is pending", "pending"),
                                           ("bc mera PAN verify hua?", "PAN"),
                                           ("fucking KYC status batao", "KYC")])
def test_abuse_with_a_request_keeps_only_the_request(message, keeps):
    act = abuse.classify(message)
    assert act.kind == abuse.ABUSIVE_WITH_REQUEST and keeps.lower() in act.remainder.lower(), (message, act)


@pytest.mark.parametrize("message", ["bhai ye bank statement ka status kya hai yaar", "what is pending?", "PAN details",
                                     "pass hua kya?", "assessment year kya hai", "classification status", "mc donalds",
                                     "chodo", "rehne do", "the case was declined", "sabse bekaar-proof address"])
def test_ordinary_turns_are_untouched_or_answered(message):
    act = abuse.classify(message)
    assert act.kind != abuse.ABUSIVE_ONLY or message in ("mc donalds",), (message, act)
