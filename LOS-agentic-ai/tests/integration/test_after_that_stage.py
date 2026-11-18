"""'who reviews it after that?' right after a how-to about a stage -> the NEXT stage (dev_en, 2026-10-15)."""
from __future__ import annotations

from tests.integration.master_env import make_case, prod  # noqa: F401
from tests.integration.test_reupload_supersedes import _store, client  # noqa: F401
from tests.integration.test_vague_messages import say


def test_after_that_following_a_stage_how_to_is_the_next_stage(client, prod):
    _, case_id = make_case(client, "Rahul Sharma")
    say(client, f"open {case_id}", "at")
    say(client, "how do I send a case from FOS to CPA?", "at")
    md = say(client, "who reviews it after that?", "at")
    assert "CPA" in md and "Next step:** Your next step is to collect" not in md, md
