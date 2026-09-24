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

    @property
    def supported(self) -> bool:
        """Whether this stage can answer anything at all yet."""
        return bool(self.knowledge_corpus or self.capabilities or self.uses_mcp)

    def answers_knowledge(self) -> bool:
        return self.knowledge_corpus is not None

    def answers_downstream(self) -> bool:
        return self.uses_mcp or bool(self.capabilities)


_NOT_BUILT = "No Copilot capability has been built for this stage yet."

#: A stage with demonstration guidance and nothing else. It can answer
#: "how does this stage work"; it cannot read a case at this stage.
_GUIDE_ONLY = (
    "A demonstration stage guide is indexed for this stage. No case "
    "capability or MCP access has been built for it yet."
)

#: Stage -> what it can do. THE SIX EMPTY ENTRIES ARE THE POINT: they are
#: registered, so the Copilot knows the stage exists and can say
#: precisely that nothing is available -- as opposed to treating it as an
#: unknown stage, which is a different failure with a different answer.
REGISTRY: dict[LosStage, StageCapabilities] = {
    LosStage.FOS: StageCapabilities(
        # The scope `knowledge_answer` already retrieves from. Named here
        # so the registry is honest about FOS being the only populated
        # stage; retrieval itself still reads its own constant.
        knowledge_corpus="FOS",
        capabilities=frozenset({
            "case_facts",
            "document_status",
            "checklist",
            "readiness",
            "case_history",
            "process_knowledge",
        }),
        uses_mcp=True,
    ),
    # THE OTHER SIX GAINED A CORPUS, AND ONLY A CORPUS. B4 indexed a
    # stage guide for every stage, so a process question about CPA or
    # RCU now has something real to answer from -- marked DEMO PROCESS
    # KNOWLEDGE, in the indexed text itself.
    #
    # They still have no `capabilities` and no MCP access: nothing
    # reads a CPA case the way the applicant agent reads a FOS one.
    # Registering the corpus without the capabilities is the honest
    # description of what exists.
    LosStage.CPA: StageCapabilities(knowledge_corpus="CPA", note=_GUIDE_ONLY),
    LosStage.CREDIT: StageCapabilities(knowledge_corpus="CREDIT",
                                       note=_GUIDE_ONLY),
    LosStage.RCU: StageCapabilities(
        knowledge_corpus="RCU",
        note=(
            "A demonstration stage guide is indexed. FindingKind.RCU also "
            "exists in case memory, but nothing produces those findings "
            "and no RCU case capability is registered."
        ),
    ),
    LosStage.BOPS: StageCapabilities(knowledge_corpus="BOPS",
                                     note=_GUIDE_ONLY),
    LosStage.HOPS: StageCapabilities(knowledge_corpus="HOPS",
                                     note=_GUIDE_ONLY),
    LosStage.DISBURSEMENT: StageCapabilities(knowledge_corpus="DISBURSEMENT",
                                             note=_GUIDE_ONLY),
}

#: Every stage is registered. A stage missing from the registry would
#: fall to the empty default and look merely unsupported, hiding the fact
#: that somebody added a stage and forgot it here.
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


__all__ = ["REGISTRY", "StageCapabilities", "capabilities_for", "mcp_tools",
           "supports", "unavailable"]
