"""
Shared caption matcher.

Three classifiers used to carry their own copy of this logic. The verification
classifier's copy built a difflib.SequenceMatcher at every offset of the text,
which cost 0.7-2.3s per call and was paid two or three times per request.
These tests pin the behaviour the replacement has to keep.
"""

from __future__ import annotations

import random

from app.core.text_match import (
    approx_contains,
    build_index,
    caption_matches,
    error_budget,
    fuzzy_contains,
)


def _edit_distance_to_any_substring(pattern: str, text: str) -> int:
    """Reference implementation: minimum edits over every substring."""

    def distance(a: str, b: str) -> int:
        previous = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            current = [i]
            for j, cb in enumerate(b, 1):
                current.append(
                    min(
                        previous[j] + 1,
                        current[j - 1] + 1,
                        previous[j - 1] + (ca != cb),
                    )
                )
            previous = current
        return previous[-1]

    best = len(pattern)
    for start in range(len(text) + 1):
        for end in range(start, len(text) + 1):
            best = min(best, distance(pattern, text[start:end]))
    return best


# ---------------------------------------------------------------------------
# fuzzy_contains: substitution only
# ---------------------------------------------------------------------------


def test_fuzzy_contains_matches_exact_window():
    assert fuzzy_contains("ELECTORSNAME", "XXELECTORSNAMEYY", 0)


def test_fuzzy_contains_tolerates_substitutions_up_to_budget():
    assert fuzzy_contains("ELECTORSNAME", "XXELECT0RSNAMEYY", 1)
    assert not fuzzy_contains("ELECTORSNAME", "XXEL3CT0RSNAMEYY", 1)


def test_fuzzy_contains_rejects_text_shorter_than_pattern():
    assert not fuzzy_contains("ELECTORSNAME", "ELECT", 5)


def test_fuzzy_contains_cannot_absorb_a_dropped_character():
    """The limitation that made the indel-tolerant path necessary."""
    assert not fuzzy_contains("IDENTITYCARD", "IENTTYCARD", 2)


# ---------------------------------------------------------------------------
# approx_contains: insertions and deletions too
# ---------------------------------------------------------------------------


def test_approx_contains_absorbs_deletions():
    # A real Voter ID scan read "IDENTITY" as "IENTTY": two characters gone.
    assert approx_contains("ELECTORPHOTOIDENTITYCARD", "IQEECTORPHOTOIENTTYCARD", 5)


def test_approx_contains_absorbs_insertions():
    # Cards print the possessive, which compacts to an extra S mid-caption.
    assert approx_contains("ELECTORPHOTOIDENTITYCARD", "ELECTORSPHOTOIDENTITYCARD", 2)


def test_approx_contains_rejects_unrelated_text():
    assert not approx_contains("INCOMETAXDEPARTMENT", "STATEMENTOFACCOUNTIFSC", 3)


def test_approx_contains_agrees_with_reference_edit_distance():
    """Randomised check against a brute-force minimum-edit-distance search."""
    random.seed(20260915)
    alphabet = "ABCDE"

    for _ in range(200):
        pattern = "".join(random.choice(alphabet) for _ in range(random.randint(3, 6)))
        text = "".join(random.choice(alphabet) for _ in range(random.randint(0, 14)))
        budget = random.randint(0, 3)

        expected = _edit_distance_to_any_substring(pattern, text) <= budget
        assert approx_contains(pattern, text, budget) is expected, (
            pattern,
            text,
            budget,
        )


# ---------------------------------------------------------------------------
# caption_matches: the policy the classifiers actually use
# ---------------------------------------------------------------------------


def test_caption_matches_prefers_exact_containment():
    assert caption_matches("STATEMENTOFACCOUNT", "XXSTATEMENTOFACCOUNTYY")


def test_caption_matches_will_not_fuzzy_match_a_short_caption():
    """A short caption must never match approximately -- it would match anything."""
    assert not caption_matches("DLNO", "DXNO")


def test_caption_matches_recovers_a_damaged_long_caption():
    assert caption_matches(
        "ELECTORPHOTOIDENTITYCARD",
        "FTE3ELECTIONCOMMISSIONOFINDIAIQEECTORPHOTOIENTTYCARDRNR0090654",
    )


def test_prefilter_never_hides_a_real_match():
    """
    The trigram prefilter is an optimisation, so it must only ever skip work
    that would have failed anyway.
    """
    random.seed(7)
    alphabet = "ABCDEFG"

    for _ in range(200):
        keyword = "".join(random.choice(alphabet) for _ in range(random.randint(12, 18)))
        text = "".join(random.choice(alphabet) for _ in range(random.randint(0, 60)))

        budget = error_budget(keyword)
        unfiltered = keyword in text or approx_contains(keyword, text, budget)
        filtered = caption_matches(keyword, text)

        if unfiltered:
            assert filtered, (keyword, text)


def test_supplied_index_matches_a_freshly_built_one():
    text = "ELECTIONCOMMISSIONOFINDIAELECTORSNAME"
    index = build_index(text)

    for caption in ("ELECTIONCOMMISSIONOFINDIA", "STATEMENTOFACCOUNT", "MARKSSTATEMENT"):
        assert caption_matches(caption, text, index=index) == caption_matches(
            caption, text
        )
