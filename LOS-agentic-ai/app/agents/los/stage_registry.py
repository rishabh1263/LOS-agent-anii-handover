"""
What each LOS stage can actually do.

THE REGISTRY REFLECTS REALITY, WHICH IS UNEVEN. FOS has a knowledge
corpus, an agent and an authorised set of MCP tools. The other six have
a DEMONSTRATION STAGE GUIDE and nothing else: they can answer "how does
this stage work" and cannot read a case. Recording the corpus without
the capabilities is the honest description -- promising a CPA
capability would produce a Copilot confidently answering CPA case
questions out of the FOS handbook.

RCU IS THE ONE WORTH SPELLING OUT. `FindingKind.RCU` exists in case
memory, so a reader could reasonably conclude RCU cases are supported.
Nothing writes those findings -- there is no RCU producer -- so the
stage carries a guide and no case capability. An enum member is not a
capability.

A DICT, NOT A CHAIN OF `if stage is ...`. Adding a stage's capabilities
is one entry; the lookups below never branch on a stage name, so the
seventh stage costs exactly what the second did.

NOTHING HERE WIDENS FOS. `knowledge_answer.STAGE` stays "FOS" and is not
read from this module -- FOS retrieval behaves exactly as it did. This
records what exists; it does not route retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.los.stages import LosStage


@dataclass(frozen=True)
class StageCapabilities:
    """
    What one stage can answer, and from what.

    Every field defaults to empty, so a stage added to the registry
    without capabilities is correctly unsupported rather than
    accidentally inheriting another stage's.
    """

    #: The retrieval scope this stage answers PROCESS_KNOWLEDGE from.
    #: None means the stage has no corpus and cannot answer one.
    knowledge_corpus: str | None = None

    #: Named capabilities this stage's desk owns.
    capabilities: frozenset[str] = field(default_factory=frozenset)

    #: Whether this stage may reach the MCP tool layer. The tool NAMES
    #: are not copied here -- `mcp_tools()` reads the live contract
    #: registry, so this cannot drift out of step with what exists.
    uses_mcp: bool = False

    #: Said out loud for a stage that is registered but not yet built,
    #: so the reason reaches a reviewer rather than living in a commit.
    note: str | None = None

    #: Stage-specific topics nothing in this build produces -- said plainly
    #: ("... information is not currently available") rather than invented.
    not_available: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        """Whether this stage can answer anything at all yet."""
        return bool(self.knowledge_corpus or self.capabilities or self.uses_mcp)

    def answers_knowledge(self) -> bool:
        return self.knowledge_corpus is not None

    def answers_downstream(self) -> bool:
        # The out-of-scope routing table is written for the FOS desk ("the
        # lending decision is made downstream, after CPA"). Only a stage
        # that serves FOS case facts routes with it; at any other stage a
        # downstream request stays CAPABILITY_UNAVAILABLE, as it always was.
        return "case_facts" in self.capabilities


#: EVERY CAPABILITY THIS BUILD CAN ACTUALLY SERVE, and what serves it.
#:
#: Configuration (`chatbot.stages.capabilities` in applicant_agent.yaml)
#: assigns capabilities to stages; a name that is not here is IGNORED and
#: logged. Configuration can switch a real capability off for a stage. It
#: cannot claim one the code does not have.
PROVIDERS: dict[str, str] = {
    "stage_status": "stages.resolve -- stage record (stage_lifecycle), then case timeline, then application status",
    "stage_history": "stages.resolve -- stage_transitions history (stage_lifecycle), else STAGE_ENTERED timeline events",
    "document_requirements": "policy engine + config/stage_requirements.yaml",
    "pending_items": "workflow.pending_items over the stage-aware checklist",
    "next_action": "workflow.next_action over the stage-aware checklist",
    "document_status": "documents.get / documents.verification",
    "case_history": "case memory -- recorded findings and decisions",
    "process_knowledge": "the stage guide corpus",
    "eligibility": "the recorded Eligibility result (eligibility_facts)",
    "case_facts": "applicant and application records",
    "checklist": "documents.checklist",
    "readiness": "workflow.readiness -- FOS to CPA handoff",
}

#: What every stage can serve from records that exist at every stage.
_COMMON = ("stage_status", "stage_history", "document_requirements",
           "pending_items", "next_action", "document_status", "case_history",
           "process_knowledge")

#: THE DEFAULTS, used when configuration names no entry for a stage. The
#: same as the shipped configuration, so a deployment without it behaves
#: identically.
_DEFAULTS: dict[str, dict[str, object]] = {
    "FOS": {"capabilities": _COMMON + ("eligibility", "case_facts",
                                       "checklist", "readiness")},
    "CPA": {"capabilities": _COMMON,
            "not_available": ("CPA assessment details",)},
    "CREDIT": {"capabilities": _COMMON + ("eligibility",),
               "not_available": ("credit decision details",)},
    "RCU": {"capabilities": _COMMON,
            "not_available": ("RCU investigation findings",)},
    "BOPS": {"capabilities": _COMMON,
             "not_available": ("BOPS processing details",)},
    "HOPS": {"capabilities": _COMMON,
             "not_available": ("HOPS approval details",)},
    "DISBURSEMENT": {"capabilities": _COMMON,
                     "not_available": ("disbursement details",)},
}


def _configured(stage: LosStage) -> StageCapabilities:
    """One stage's capabilities: configuration over the defaults, filtered
    to what a provider in this build can actually serve."""
    import logging

    settings: dict[str, object] = dict(_DEFAULTS.get(stage.value, {}))
    try:
        from app.agents.applicant import config

        override = (config.chatbot("stages").get("capabilities") or {}).get(
            stage.value)
        if isinstance(override, dict):
            settings.update(override)
    except Exception:  # pragma: no cover - configuration failure
        pass

    if settings.get("enabled") is False:
        return StageCapabilities(
            knowledge_corpus=None,
            note=f"The {stage.value} stage is disabled in configuration.")

    names: set[str] = set()
    for name in settings.get("capabilities") or ():
        key = str(name).strip().lower()
        if key in PROVIDERS:
            names.add(key)
        else:
            logging.getLogger(__name__).warning(
                "Stage %s: capability %r has no provider in this build; "
                "ignored", stage.value, name)

    missing = tuple(str(t) for t in settings.get("not_available") or ())
    return StageCapabilities(
        knowledge_corpus=(stage.value if "process_knowledge" in names
                          else None),
        capabilities=frozenset(names),
        # The MCP case tools read a case's own records, which exist at every
        # stage; a stage that can serve case facts or document status may
        # reach them.
        uses_mcp=bool(names & {"case_facts", "document_status",
                               "pending_items", "document_requirements"}),
        not_available=missing,
        note=(f"Not built for this stage: {', '.join(missing)}."
              if missing else None),
    )


class _Registry(dict):
    """Stage -> capabilities, read from configuration on every lookup so a
    configuration reload takes effect without a restart."""

    def get(self, stage, default=None):  # type: ignore[override]
        if stage in set(LosStage):
            return _configured(stage)
        return default

    def __getitem__(self, stage):
        return _configured(stage)


#: Every stage is registered; what each can serve comes from `_configured`.
REGISTRY: dict[LosStage, StageCapabilities] = _Registry(
    {stage: None for stage in LosStage})

assert set(REGISTRY) == set(LosStage), "every LosStage needs a registry entry"

_NONE = StageCapabilities(note="This stage is not registered.")


def capabilities_for(stage: LosStage | None) -> StageCapabilities:
    """What this stage can do. Empty capabilities for an unresolved stage."""
    if stage is None:
        return _NONE
    return REGISTRY.get(stage, _NONE)


def supports(stage: LosStage | None) -> bool:
    return capabilities_for(stage).supported


def mcp_tools(stage: LosStage | None) -> tuple[str, ...]:
    """
    The MCP tools this stage may reach.

    READ FROM THE LIVE CONTRACTS, never copied. A second list of tool
    names here would be a second answer to "which tools exist", and the
    two would drift the first time a tool was added.
    """
    if not capabilities_for(stage).uses_mcp:
        return ()

    from app.mcp.contracts import CONTRACTS

    return tuple(sorted(CONTRACTS))


def unavailable(stage: LosStage | None) -> dict[str, str]:
    """
    The deterministic answer for a stage that cannot serve this request.

    A DISTINCT OUTCOME, deliberately. It is not an authorisation failure
    (the caller may well be entitled), not missing case data (the case
    may be complete), and not an unrecognised intent (the question was
    understood perfectly). Conflating it with any of those would send a
    reviewer to fix the wrong thing.
    """
    name = stage.value if stage is not None else "UNRESOLVED"
    registered = capabilities_for(stage)

    message = (
        f"No {name} Copilot capability is currently registered for this "
        "request."
        if stage is not None else
        "The stage for this case could not be determined, so no Copilot "
        "capability could be selected for it."
    )

    published = {"status": "CAPABILITY_UNAVAILABLE", "message": message}
    if stage is not None:
        published["stage"] = name
    if registered.note:
        published["detail"] = registered.note
    return published


__all__ = ["PROVIDERS", "REGISTRY", "StageCapabilities", "capabilities_for",
           "mcp_tools", "supports", "unavailable"]
