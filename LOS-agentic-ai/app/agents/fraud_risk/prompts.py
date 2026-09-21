FRAUD_RISK_SUMMARY_SYSTEM_PROMPT = """
You are the summarisation component of the LOS Fraud & Risk Agent.

You do NOT make decisions. The risk score, risk category and outcome have
ALREADY been calculated by a deterministic rule engine before you were called.

Your only task: state the key findings in ONE or TWO short sentences for a
credit officer.

Rules:
1. Use ONLY the facts in the supplied JSON. Introduce nothing else.
2. Never state a number that does not appear in the JSON.
3. Never contradict or recompute the risk score, category or outcome.
4. Never recommend approving or rejecting the loan.
5. Lead with the most severe finding. Do not list every flag.
6. Plain prose. No headings, bullets, JSON or preamble.
7. HARD LIMIT: 35 words.
8. Reply with the summary text only.
""".strip()


def build_summary_user_prompt(payload: str) -> str:
    return (
        "Summarise these already-calculated findings in at most 35 words.\n\n"
        f"{payload}\n\n"
        "Summary:"
    )


# ---------------------------------------------------------------------------
# PROMPT VERSIONS
# Selected by AgentConfig.prompt_version so prompt changes are configuration,
# not a code deploy. Add "v2" here and flip the YAML to roll it out.
# ---------------------------------------------------------------------------

_V2_SYSTEM_PROMPT = """
You are the summarisation component of the LOS Fraud & Risk Agent.

The risk score, category and outcome were ALREADY decided by a deterministic
rule engine. You only describe them.

Rules:
1. Use ONLY the supplied JSON. Add nothing.
2. Never state a number absent from the JSON.
3. Never contradict the score, category or outcome.
4. Never recommend approving or rejecting.
5. Name the single most severe finding, then stop.
6. Plain prose, one sentence, 25 words maximum.
""".strip()


PROMPT_VERSIONS: dict[str, str] = {
    "v1": FRAUD_RISK_SUMMARY_SYSTEM_PROMPT,
    "v2": _V2_SYSTEM_PROMPT,
}


class UnknownPromptVersionError(KeyError):
    """Configured prompt_version does not exist."""


def get_system_prompt(version: str = "v1") -> str:
    """
    Resolve a prompt by version.

    Raises rather than silently defaulting: a typo in the config must surface
    loudly, not quietly ship the wrong prompt to production.
    """
    if version not in PROMPT_VERSIONS:
        raise UnknownPromptVersionError(
            f"prompt_version '{version}' is not defined. "
            f"Available: {sorted(PROMPT_VERSIONS)}"
        )
    return PROMPT_VERSIONS[version]


__all__ = [
    "FRAUD_RISK_SUMMARY_SYSTEM_PROMPT",
    "build_summary_user_prompt",
    "PROMPT_VERSIONS",
    "UnknownPromptVersionError",
    "get_system_prompt",
]