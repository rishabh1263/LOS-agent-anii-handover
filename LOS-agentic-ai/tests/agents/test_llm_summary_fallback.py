"""
The LOS summary, and what happens when the model is not there.

THE RULE THIS SUITE DEFENDS: the summary is the last thing that happens, it
is a sentence, and nothing downstream reads it. Verification, extraction, KYC
and the final status are all computed before the model is consulted, so a
model that is slow, absent or talking nonsense may cost the sentence and may
cost nothing else.

The bug these tests pin: the fallback was correct but LATE. An unreachable
provider was only discovered when the generation timeout expired, so every
request paid the full timeout -- about 8 seconds added to a 2.7 second call
-- for a sentence the deterministic path writes for free.
"""

from __future__ import annotations

import time

import pytest

from app.agents.los import summary as summary_module
from app.llm import availability


@pytest.fixture(autouse=True)
def _reset_availability():
    """The cached verdict is process-wide; clear it around every test."""
    availability.reset()
    yield
    availability.reset()


def envelope() -> dict:
    """A finished application result, of the shape the summary describes."""
    return {
        "request_id": "REQ-1",
        "applicant_id": "APP-1",
        "status": "SUCCESS",
        "documents": [
            {
                "source_id": "pan.jpg",
                "status": "SUCCESS",
                "document": {"type": "PAN", "category": "IDENTITY", "supported": True},
                "verification": {"status": "PASS"},
                "extraction": {"fields": {"pan_number": "ABCDE1234F"}},
                "processing": {},
                "errors": [],
            }
        ],
        "kyc": {"status": "PASS", "reason_codes": []},
        "processing": {},
        "errors": [],
    }


# ==========================================================================
# THE SWITCH
# ==========================================================================


async def test_a_disabled_model_uses_the_deterministic_summary():
    text, source = await summary_module.build_summary_async(
        envelope(), use_llm=False
    )

    assert text
    assert source != "llm"


async def test_a_disabled_model_costs_nothing(monkeypatch):
    """Switched off means not called, not called-and-discarded."""

    async def explode(payload):
        raise AssertionError("the model must not be called when disabled")

    monkeypatch.setattr(summary_module, "_agenerate", explode)

    text, _source = await summary_module.build_summary_async(
        envelope(), use_llm=False
    )

    assert text


# ==========================================================================
# THE MODEL IS AVAILABLE
# ==========================================================================


async def test_an_available_model_writes_the_summary(monkeypatch):
    async def generate(payload):
        return "One PAN was verified and KYC passed."

    monkeypatch.setattr(summary_module, "_agenerate", generate)

    text, source = await summary_module.build_summary_async(
        envelope(), use_llm=True
    )

    assert source == "llm"
    assert "PAN" in text


# ==========================================================================
# THE MODEL IS NOT THERE
# ==========================================================================


async def test_an_unreachable_provider_falls_back(monkeypatch):
    monkeypatch.setattr(availability, "provider_reachable", lambda: False)

    text, source = await summary_module.build_summary_async(
        envelope(), use_llm=True
    )

    assert text
    assert source != "llm"


async def test_an_unreachable_provider_is_detected_before_the_timeout(monkeypatch):
    """
    The whole point of the fix.

    The generation call must never be reached when the provider is down, so
    the request cannot pay the generation timeout to learn it.
    """
    monkeypatch.setattr(availability, "provider_reachable", lambda: False)

    async def explode(*args, **kwargs):
        raise AssertionError("generation attempted against a dead provider")

    monkeypatch.setattr(
        "app.llm.provider.create_ollama_client",
        lambda: (_ for _ in ()).throw(AssertionError("client built for a dead provider")),
    )

    started = time.perf_counter()
    text, source = await summary_module.build_summary_async(
        envelope(), use_llm=True
    )
    elapsed = time.perf_counter() - started

    assert text
    assert source != "llm"
    assert elapsed < 1.0, f"fallback took {elapsed:.2f}s; it should be immediate"


async def test_a_timeout_falls_back(monkeypatch):
    import asyncio

    monkeypatch.setattr(availability, "provider_reachable", lambda: True)

    async def hang(payload):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(summary_module, "_agenerate", hang)

    text, source = await summary_module.build_summary_async(
        envelope(), use_llm=True
    )

    assert text
    assert source != "llm"


async def test_a_malformed_response_falls_back(monkeypatch):
    """A model that answers with the wrong shape is a model that failed."""

    async def nonsense(payload):
        raise ValueError("model response carried no text")

    monkeypatch.setattr(summary_module, "_agenerate", nonsense)

    text, source = await summary_module.build_summary_async(
        envelope(), use_llm=True
    )

    assert text
    assert source != "llm"


# ==========================================================================
# THE CACHED VERDICT
# ==========================================================================


def test_a_failed_probe_is_remembered():
    availability.mark_unavailable("test")

    assert availability.cached_state() == "UNAVAILABLE"
    assert availability.provider_reachable() is False


def test_the_cached_verdict_skips_further_probing(monkeypatch):
    """
    A ten-document application must pay the probe once, not ten times.
    """
    availability.mark_unavailable("test")

    def explode(*args, **kwargs):
        raise AssertionError("probed again while the verdict was cached")

    monkeypatch.setattr("socket.create_connection", explode)

    for _ in range(10):
        assert availability.provider_reachable() is False


def test_the_cooldown_can_be_configured(monkeypatch):
    monkeypatch.setenv("LLM_UNAVAILABLE_COOLDOWN_SECONDS", "0")
    availability.mark_unavailable("test")

    # A zero cooldown means the next call re-probes rather than trusting the
    # cache, so recovery is noticed immediately where that is wanted.
    assert availability.cached_state() != "UNAVAILABLE"


def test_the_connect_timeout_is_short_by_default():
    """A connection that has not landed in half a second will not help."""
    assert availability.connect_timeout_seconds() <= 1.0


def test_the_generation_timeout_is_short_by_default():
    """
    Bounds the case the probe cannot catch: a socket that accepts and a model
    that then hangs, which is what a provider without its model pulled does.
    """
    assert summary_module.llm_timeout_seconds() <= 5.0


async def test_a_generation_failure_marks_the_provider_down(monkeypatch):
    """
    A failure found the expensive way must spare the next request.

    The probe cannot predict a socket that accepts and then misbehaves, so
    the generation path records it instead.
    """
    monkeypatch.setattr(availability, "provider_reachable", lambda: True)

    class Boom:
        async def get_response(self, *args, **kwargs):
            raise RuntimeError("model exploded")

    monkeypatch.setattr("app.llm.provider.create_ollama_client", lambda: Boom())

    with pytest.raises(RuntimeError):
        await summary_module._agenerate({"documents": []})

    assert availability.cached_state() == "UNAVAILABLE"


async def test_one_attempt_per_request(monkeypatch):
    """
    No retry loop.

    The caller already holds a correct answer; retrying spends a reviewer's
    latency for the chance of a slightly nicer sentence.
    """
    monkeypatch.setattr(availability, "provider_reachable", lambda: True)

    attempts = {"count": 0}

    class Counting:
        async def get_response(self, *args, **kwargs):
            attempts["count"] += 1
            raise RuntimeError("model exploded")

    monkeypatch.setattr(
        "app.llm.provider.create_ollama_client", lambda: Counting()
    )

    text, source = await summary_module.build_summary_async(
        envelope(), use_llm=True
    )

    assert attempts["count"] == 1
    assert text
    assert source != "llm"


# ==========================================================================
# THE MODEL CANNOT CHANGE A RESULT
# ==========================================================================


@pytest.mark.parametrize("reachable", [True, False])
async def test_the_model_never_alters_the_decision(monkeypatch, reachable):
    """
    Whatever the model does, the computed result is untouched.

    The summary is written from an envelope that is already final; this
    asserts the envelope that goes in comes back out unchanged.
    """
    monkeypatch.setattr(availability, "provider_reachable", lambda: reachable)

    async def mischief(payload):
        return "Everything was rejected and KYC failed."

    monkeypatch.setattr(summary_module, "_agenerate", mischief)

    payload = envelope()
    before = {
        "status": payload["status"],
        "kyc": dict(payload["kyc"]),
        "verification": dict(payload["documents"][0]["verification"]),
        "extraction": dict(payload["documents"][0]["extraction"]),
    }

    await summary_module.build_summary_async(payload, use_llm=True)

    assert payload["status"] == before["status"]
    assert payload["kyc"] == before["kyc"]
    assert payload["documents"][0]["verification"] == before["verification"]
    assert payload["documents"][0]["extraction"] == before["extraction"]


# ==========================================================================
# SLOW IS NOT ABSENT
#
# A provider that answers late is a different thing from one that is not
# there, and conflating them was expensive: a single request that overran the
# 1.5s budget by 3 ms used to suppress the model for the next 60 seconds --
# fifteen consecutive requests, every one of which had time to spare.
# ==========================================================================

def test_a_slow_generation_is_not_treated_as_an_absent_provider():
    """The two failures earn different hold-offs."""
    assert availability.slow_cooldown_seconds() < \
        availability.unavailable_cooldown_seconds()


def test_the_slow_cooldown_is_short_by_default():
    """Short enough that ordinary request spacing outlives it."""
    assert availability.slow_cooldown_seconds() <= 5.0


def test_the_slow_cooldown_can_be_configured(monkeypatch):
    monkeypatch.setenv("LLM_SLOW_COOLDOWN_SECONDS", "0")
    availability.mark_slow("test")

    # Zero means attempt the model on the very next request.
    assert availability.cached_state() != "UNAVAILABLE"


def test_marking_slow_still_suppresses_the_next_attempt(monkeypatch):
    """A burst must not each rediscover the same slowness."""
    monkeypatch.setenv("LLM_SLOW_COOLDOWN_SECONDS", "30")
    availability.mark_slow("test")

    assert availability.cached_state() == "UNAVAILABLE"
    assert availability.provider_reachable() is False


async def test_a_generation_timeout_uses_the_short_cooldown(monkeypatch):
    """
    The path that actually matters: generation overran, so the NEXT request
    should be held off briefly rather than for the unreachable window.
    """
    monkeypatch.setattr(availability, "provider_reachable", lambda: True)
    monkeypatch.setenv("LLM_SLOW_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("LLM_UNAVAILABLE_COOLDOWN_SECONDS", "600")

    class Hangs:
        async def get_response(self, *args, **kwargs):
            raise TimeoutError()

    monkeypatch.setattr("app.llm.provider.create_ollama_client", lambda: Hangs())

    with pytest.raises(TimeoutError):
        await summary_module._agenerate({"documents": []})

    # Had this gone through mark_unavailable it would now be suppressed for
    # ten minutes.
    assert availability.cached_state() != "UNAVAILABLE"


# ==========================================================================
# WHAT THE MODEL IS SHOWN, AND WHAT IT MAY SAY BACK
# ==========================================================================

def test_the_payload_carries_the_status_counts():
    """
    Worked out here so the model does not have to.

    Left to itself it writes "3 succeeded, 1 rejected", and those derived
    numbers appear nowhere in what it was given, so the whole sentence was
    discarded and the summary fell back.
    """
    payload = summary_module.build_llm_payload({
        "status": "REVIEW",
        "documents": [
            {"source_id": "a", "status": "SUCCESS"},
            {"source_id": "b", "status": "SUCCESS"},
            {"source_id": "c", "status": "REVIEW"},
            {"source_id": "d", "status": "REJECTED"},
        ],
        "kyc": {"status": "REVIEW", "reason_codes": []},
        "errors": [],
    })

    assert payload["document_status_counts"] == {
        "SUCCESS": 2, "REVIEW": 1, "REJECTED": 1,
    }


def test_the_payload_still_carries_no_extracted_values():
    """Statuses and counts only. What it is never shown it cannot leak."""
    payload = summary_module.build_llm_payload(envelope())
    blob = str(payload)

    assert "ABCDE1234F" not in blob
    assert "fields" not in payload


def test_a_document_level_status_is_a_computed_verdict():
    """
    Quoting document_statuses is quoting the data, not inventing an outcome.

    This was rejecting valid summaries: the model said "one document was
    REJECTED" -- which the envelope said too -- and the sentence was thrown
    away as an uncomputed verdict.
    """
    env = {
        "status": "REVIEW",
        "documents": [
            {"source_id": "a", "status": "SUCCESS"},
            {"source_id": "b", "status": "REJECTED"},
        ],
        "kyc": {"status": "REVIEW", "reason_codes": []},
        "errors": [],
    }

    accepted, value = summary_module.validate_llm_summary(
        "Of two documents one is SUCCESS and one is REJECTED; KYC is in REVIEW.",
        env,
    )
    assert accepted is True, value


def test_a_verdict_the_pipeline_never_computed_is_still_refused():
    """The set widened to what was computed -- it did not stop checking."""
    env = {
        "status": "REVIEW",
        "documents": [{"source_id": "a", "status": "SUCCESS"}],
        "kyc": {"status": "REVIEW", "reason_codes": []},
        "errors": [],
    }

    accepted, reason = summary_module.validate_llm_summary(
        "The application was checked and the outcome was FAILED.", env,
    )
    assert accepted is False
    assert "uncomputed verdict" in reason


def test_a_number_the_pipeline_never_computed_is_still_refused():
    env = {
        "status": "SUCCESS",
        "documents": [{"source_id": "a", "status": "SUCCESS"}],
        "kyc": {"status": "PASS", "reason_codes": []},
        "errors": [],
    }

    accepted, reason = summary_module.validate_llm_summary(
        "One document passed and the applicant earns 4200000 rupees.", env,
    )
    assert accepted is False
    assert "unsupported number" in reason


# ==========================================================================
# KEEPING THE MODEL RESIDENT
# ==========================================================================

def test_the_generation_options_ask_ollama_to_keep_the_model_loaded():
    """
    Ollama unloads an idle model after five minutes by default, and loading
    qwen2.5:3b back was measured at 2281 ms -- over budget before a single
    token is generated, which then marks the provider down for the cooldown.
    """
    options = summary_module._generation_options()

    assert options["keep_alive"] == summary_module.keep_alive()
    assert options["max_tokens"] == summary_module.summary_max_tokens()


def test_keep_alive_can_be_configured(monkeypatch):
    monkeypatch.setenv("LOS_LLM_KEEP_ALIVE", "2h")
    assert summary_module.keep_alive() == "2h"


def test_keep_alive_has_a_default(monkeypatch):
    monkeypatch.delenv("LOS_LLM_KEEP_ALIVE", raising=False)
    assert summary_module.keep_alive() == "30m"
