"""
Configuration for the Applicant Agent.

Reads app/config/applicant_agent.yaml, cached, with a per-flag environment
override in the shape the rest of the service already uses
(APPLICANT_AGENT_<NAME>). Same mechanism as app/services/verification_config.py
and app/agents/los/config.py -- one convention, not a fourth one.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: dict[str, Any] | None = None

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "applicant_agent.yaml"


def config_path() -> Path:
    return Path(os.getenv("APPLICANT_AGENT_CONFIG_PATH") or _DEFAULT_PATH)


def _load() -> dict[str, Any]:
    global _CACHE

    if _CACHE is not None:
        return _CACHE

    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        path = config_path()
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            logger.error("Applicant Agent config not found at %s", path)
            loaded = {}
        except yaml.YAMLError as exc:
            logger.error("Applicant Agent config is not valid YAML: %s", exc)
            loaded = {}
        _CACHE = loaded
        return _CACHE


def reload() -> None:
    """
    Drop the cache. For tests and for an explicit reconfiguration.

    THE POLICY CACHE GOES WITH IT. `checklist_for` now resolves through
    the policy engine, so reloading this file alone left half the
    configuration stale -- a reconfiguration that changed a product's
    documents would take effect for some callers and not others, which is
    worse than not reloading at all.
    """
    global _CACHE
    with _LOCK:
        _CACHE = None
    try:
        from app.agents.applicant import normalize, semantic

        normalize.reload()
        semantic.reload()
    except Exception:  # pragma: no cover - import failure
        logger.exception("Could not reload the normalisation tables")
    try:
        from app.agents.policy import loader as policy_loader

        policy_loader.reload()
    except Exception:  # pragma: no cover - import failure
        logger.exception("Could not reload the document policies")


def _section(name: str) -> dict[str, Any]:
    return _load().get(name, {}) or {}


def _flag(section: str, name: str, default: bool) -> bool:
    """Environment wins over YAML; YAML wins over the built-in default."""
    override = (os.getenv(f"APPLICANT_AGENT_{name.upper()}") or "").strip().lower()
    if override in {"true", "1", "yes", "on"}:
        return True
    if override in {"false", "0", "no", "off"}:
        return False
    return bool(_section(section).get(name, default))


# -- agent ------------------------------------------------------------------

def agent_name() -> str:
    return str(_section("agent").get("name") or "Applicant Agent")


def enabled() -> bool:
    return _flag("agent", "enabled", True)


def llm_enabled() -> bool:
    return _flag("agent", "llm_enabled", True)


def llm_for_simple_intents() -> bool:
    return _flag("agent", "llm_for_simple_intents", False)


def temperature() -> float:
    """`chatbot.llm.temperature` when set, else `agent.temperature`."""
    raw = chatbot("llm").get("temperature",
                             _section("agent").get("temperature", 0.1))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.1


# -- workflow ---------------------------------------------------------------

def workflow_states() -> list[str]:
    states = _section("workflow").get("states")
    if isinstance(states, list) and states:
        return [str(s) for s in states]
    return [
        "APPLICATION_CREATED", "DOCUMENT_COLLECTION",
        "BASIC_DOCUMENT_VERIFICATION", "READY_FOR_CPA",
    ]


# -- document checklist -----------------------------------------------------

def products() -> list[str]:
    """
    Every product with a declared checklist, including `default`.

    Used to answer taxonomy-wide questions -- what does slot X accept
    anywhere -- without a caller having to guess the product names.

    BOTH SOURCES. A product may be described by a policy file, by this
    file, or by both. Leaving the policy products out meant a question
    naming a product that had been moved to a policy file stopped
    recognising it, and the question was answered from the `default`
    checklist instead -- a shorter list, presented with no sign that it was
    the wrong one.
    """
    from app.agents.policy import loader as policy_loader

    names = [str(key).upper() if key != "default" else "default"
             for key in _section("documents")]
    try:
        declared = policy_loader.known_products()
    except Exception:  # pragma: no cover - configuration failure
        logger.exception("Could not list the configured policies")
        declared = []
    for product in declared:
        if product not in names:
            names.append(product)
    return names


def checklist_for(product: str | None) -> list[dict[str, Any]]:
    """
    The document checklist for a product, normalised.

    Returns one entry per slot: {"slot", "accepts", "mandatory"}. Required and
    optional slots come back in the same list with a flag, because the
    checklist a FOS is shown includes both -- an optional document that has
    been collected should be visible, and one that has not should not be
    mistaken for a blocker.

    THE POLICY FILE WINS WHERE THERE IS ONE, and this is the whole reason
    the delegation exists. Two configuration files describing the same
    product will eventually disagree, and when they did, the /config
    endpoint advertised one checklist while a real case was measured
    against another. There is one resolution path now; this is a view onto
    it.

    NO LOAN AMOUNT IS ASSUMED. This is the PRODUCT-level answer -- what
    every application for this product needs, whatever the amount. A case's
    own checklist comes from `workflow.resolution_for`, which has the
    amount and the applicant's attributes. Defaulting an amount here would
    put a band's documents into the answer to "what does a personal loan
    need?", which is a question about the product.

    Falls back to `default` when the product is unset or unknown, so a case
    that has not chosen a product still has something to be measured against
    rather than appearing complete by accident.
    """
    from app.agents.policy import engine as policy

    try:
        resolution = policy.resolve(product)
    except Exception:  # pragma: no cover - configuration failure
        logger.exception("Policy resolution failed for %s", product)
    else:
        if resolution.policy_id != policy.LEGACY_POLICY_ID:
            return [{"slot": r.slot, "accepts": list(r.accepts),
                     "mandatory": r.mandatory}
                    for r in resolution.requirements]

    return _checklist_from_yaml(product)


def _checklist_from_yaml(product: str | None) -> list[dict[str, Any]]:
    """
    The checklist as declared in this file.

    Called directly by the policy engine's fallback, so it must not
    delegate back to `checklist_for`.
    """
    documents = _section("documents")
    key = (product or "").strip().upper()
    config = (documents.get(key) if key in documents else None)
    if config is None:
        config = documents.get("default", {}) or {}

    entries: list[dict[str, Any]] = []
    for mandatory, group in ((True, "required"), (False, "optional")):
        for item in (config.get(group) or []):
            # A bare string is accepted as shorthand for a slot that accepts
            # only the type of the same name.
            if isinstance(item, str):
                slot, accepts = item.upper(), [item.upper()]
            else:
                slot = str(item.get("slot") or "").upper()
                accepts = [str(a).upper() for a in (item.get("accepts") or [])]
                if slot and not accepts:
                    accepts = [slot]
            if slot and accepts:
                entries.append(
                    {"slot": slot, "accepts": accepts, "mandatory": mandatory}
                )
    return entries


def document_types() -> list[str]:
    """
    The FOS-stage taxonomy: every type that may be uploaded or selected.

    Naming a type here does NOT make it required anywhere. A product becomes
    subject to a type only by listing a slot that accepts it.
    """
    declared = _load().get("document_types")
    if isinstance(declared, list) and declared:
        return [str(t).upper() for t in declared]
    return ["PAN", "DRIVING_LICENCE", "PASSPORT", "VOTER_ID", "BANK_STATEMENT"]


# -- readiness --------------------------------------------------------------

def readiness_rules() -> dict[str, bool]:
    section = _section("readiness")
    return {
        "require_applicant_fields": bool(section.get("require_applicant_fields", True)),
        "require_application_fields": bool(section.get("require_application_fields", True)),
        "require_all_documents": bool(section.get("require_all_documents", True)),
        "require_documents_verified": bool(section.get("require_documents_verified", True)),
        "block_on_review": bool(section.get("block_on_review", True)),
    }


# -- permissions ------------------------------------------------------------

def permissions_enforced() -> bool:
    return _flag("permissions", "enforce", True)


def read_all_scope() -> str:
    return str(_section("permissions").get("read_all_scope") or "los.read")


def write_all_scope() -> str:
    """The service scope that may read AND write any case (`los.write`)."""
    return str(_section("permissions").get("write_all_scope") or "los.write")


def read_scopes() -> dict[str, str]:
    return dict(_section("permissions").get("read", {}) or {})


def write_scopes() -> dict[str, str]:
    return dict(_section("permissions").get("write", {}) or {})


def denied_capabilities() -> list[str]:
    return [str(x) for x in (_section("permissions").get("denied") or [])]


# -- routing ----------------------------------------------------------------

def routing_table() -> dict[str, dict[str, str]]:
    return dict(_section("routing") or {})


# -- timeouts ---------------------------------------------------------------

def _seconds(name: str, default: float) -> float:
    override = os.getenv(f"APPLICANT_AGENT_{name.upper()}")
    raw = override if override is not None else _section("timeouts").get(name, default)
    try:
        return max(0.1, float(raw))
    except (TypeError, ValueError):
        return default


def tool_timeout_seconds() -> float:
    return _seconds("tool_seconds", 5.0)


def llm_timeout_seconds() -> float:
    """`chatbot.llm.timeout_seconds` when set, else `timeouts.llm_seconds`.
    The environment override APPLICANT_AGENT_LLM_SECONDS still wins."""
    if os.getenv("APPLICANT_AGENT_LLM_SECONDS") is None:
        raw = chatbot("llm").get("timeout_seconds")
        if raw is not None:
            try:
                return max(0.1, float(raw))
            except (TypeError, ValueError):
                pass
    return _seconds("llm_seconds", 1.5)


# -- mcp runtime ------------------------------------------------------------

def _mcp(name: str, default: str) -> str:
    override = (os.getenv(f"LOS_MCP_{name.upper()}") or "").strip()
    return (override or str(_section("mcp").get(name) or default)).strip()


def mcp_mode() -> str:
    """in_process or protocol. Anything else is treated as in_process."""
    mode = _mcp("mode", "in_process").lower()
    return mode if mode in {"in_process", "protocol"} else "in_process"


def mcp_transport() -> str:
    transport = _mcp("transport", "memory").lower()
    return transport if transport in {"memory", "stdio", "http"} else "memory"


def mcp_server_url() -> str:
    return _mcp("server_url", "http://127.0.0.1:8030/mcp")


def mcp_fallback() -> str:
    fallback = _mcp("fallback", "fail").lower()
    return fallback if fallback in {"fail", "in_process"} else "fail"


def _mcp_seconds(name: str, default: float) -> float:
    try:
        return max(0.1, float(_mcp(name, str(default))))
    except (TypeError, ValueError):
        return default


def compose_case_answers() -> bool:
    """Whether the Copilot has Qwen phrase structured case answers."""
    return llm_enabled() and bool(chatbot("compose").get("case_answers", False))


def compose_request_budget_seconds() -> float:
    """
    The whole request's budget for reaching a model: composition is skipped
    when less than a second of it is left, and never waits past it.
    """
    try:
        return max(1.0, float(chatbot("compose").get("request_budget_seconds", 8.0)))
    except (TypeError, ValueError):
        return 8.0


def compose_timeout_seconds() -> float:
    try:
        return max(0.5, float(chatbot("compose").get("timeout_seconds", 6.0)))
    except (TypeError, ValueError):
        return 6.0


def mcp_call_timeout_seconds() -> float:
    return _mcp_seconds("call_timeout_seconds", 5.0)


def mcp_max_result_bytes() -> int:
    """The largest tool result the MCP boundary will carry, either way."""
    try:
        return max(1024, int(float(_mcp("max_result_bytes", "262144"))))
    except (TypeError, ValueError):
        return 262144


def mcp_allowed_tools() -> frozenset[str] | None:
    """
    The tools the MCP server may expose, when configuration narrows them.
    None: every READ contract. Never widens beyond the read contracts --
    a name here that is not one is ignored, not registered.
    """
    raw = _section("mcp").get("allowed_tools")
    override = (os.getenv("LOS_MCP_ALLOWED_TOOLS") or "").strip()
    if override:
        raw = [t.strip() for t in override.split(",")]
    if not raw:
        return None
    return frozenset(str(t).strip() for t in raw if str(t).strip())


def mcp_config_errors() -> list[str]:
    """
    What is WRONG with the MCP configuration, as written. `mcp_mode()` and
    friends fall back to safe defaults so a typo can never widen anything;
    this is what lets readiness say MISCONFIGURED instead of pretending the
    fallback was intended.
    """
    problems = []
    checks = (("mode", {"in_process", "protocol"}),
              ("transport", {"memory", "stdio", "http"}),
              ("fallback", {"fail", "in_process"}))
    for name, allowed in checks:
        raw = _mcp(name, "").lower()
        if raw and raw not in allowed:
            problems.append(f"mcp.{name}={raw!r} is not one of "
                            f"{sorted(allowed)}")
    if mcp_mode() == "protocol" and mcp_transport() == "http":
        url = mcp_server_url()
        if not url.lower().startswith(("http://", "https://")):
            problems.append("mcp.server_url must be an http(s) URL for the "
                            "http transport")
    return problems


def mcp_connect_timeout_seconds() -> float:
    return _mcp_seconds("connect_timeout_seconds", 20.0)


# -- universal copilot ------------------------------------------------------

def chatbot(section: str) -> dict[str, Any]:
    """One subsection of `chatbot:`. Empty when absent, never None."""
    value = (_section("chatbot") or {}).get(section) or {}
    return value if isinstance(value, dict) else {}


def _chatbot_flag(section: str, name: str, default: bool) -> bool:
    value = chatbot(section).get(name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def _chatbot_int(section: str, name: str, default: int) -> int:
    try:
        return int(chatbot(section).get(name, default))
    except (TypeError, ValueError):
        return default


def max_output_tokens() -> int:
    return max(16, _chatbot_int("llm", "max_output_tokens", 96))


def max_sentences() -> int:
    return max(1, _chatbot_int("response", "max_sentences", 3))


def max_characters() -> int:
    return max(40, _chatbot_int("response", "max_characters", 700))


def expose_internal_ids() -> bool:
    return _chatbot_flag("grounding", "expose_internal_ids", False)


def expose_internal_reason_codes() -> bool:
    return _chatbot_flag("grounding", "expose_internal_reason_codes", False)


def validation(name: str, default: bool = True) -> bool:
    """A `chatbot.validation` switch; `enabled: false` turns every one off."""
    if not _chatbot_flag("validation", "enabled", True):
        return False
    return _chatbot_flag("validation", name, default)


def regenerate_attempts() -> int:
    return max(0, min(2, _chatbot_int("validation", "regenerate_attempts", 0)))


def fallback(kind: str) -> str:
    """Only `structured_answer` exists; anything else is treated as it."""
    return str(chatbot("fallback").get(kind) or "structured_answer")


def stage_label(stage: str | None) -> str:
    """How a stage is named in an answer: configured label, else its name."""
    key = str(stage or "").upper()
    labels = chatbot("stages").get("labels") or {}
    return str(labels.get(key) or key.replace("_", " ").title() or "unknown")


def jev_enabled() -> bool:
    """JEV_ENABLED in the environment wins over `chatbot.jev.enabled`."""
    override = (os.getenv("JEV_ENABLED") or "").strip().lower()
    if override in {"true", "1", "yes", "on"}:
        return True
    if override in {"false", "0", "no", "off"}:
        return False
    return _chatbot_flag("jev", "enabled", False)


def jev_timeout_seconds() -> float:
    try:
        return max(0.05, float(chatbot("jev").get("timeout_seconds", 1.0)))
    except (TypeError, ValueError):
        return 1.0


def total_timeout_seconds() -> float:
    return _seconds("total_seconds", 30.0)


def snapshot() -> dict[str, Any]:
    """Every resolved setting. Reported at startup, not guessed at."""
    return {
        "name": agent_name(),
        "enabled": enabled(),
        "llm_enabled": llm_enabled(),
        "llm_for_simple_intents": llm_for_simple_intents(),
        "permissions_enforced": permissions_enforced(),
        "workflow_states": workflow_states(),
        "readiness_rules": readiness_rules(),
        "routes": sorted(routing_table()),
        "tool_timeout_seconds": tool_timeout_seconds(),
        "llm_timeout_seconds": llm_timeout_seconds(),
        "llm_max_output_tokens": max_output_tokens(),
        "response_max_sentences": max_sentences(),
        "response_max_characters": max_characters(),
        "validation_enabled": validation("enabled"),
        "regenerate_attempts": regenerate_attempts(),
        "jev_enabled": jev_enabled(),
        "compose_case_answers": compose_case_answers(),
        "compose_timeout_seconds": compose_timeout_seconds(),
        "mcp_mode": mcp_mode(),
        "mcp_transport": mcp_transport(),
        "mcp_fallback": mcp_fallback(),
        "mcp_call_timeout_seconds": mcp_call_timeout_seconds(),
    }


__all__ = [
    "agent_name", "checklist_for", "denied_capabilities", "enabled",
    "llm_enabled", "llm_for_simple_intents", "llm_timeout_seconds",
    "permissions_enforced", "read_all_scope", "read_scopes",
    "readiness_rules", "reload", "routing_table", "snapshot", "temperature",
    "tool_timeout_seconds", "total_timeout_seconds", "workflow_states",
    "write_scopes",
]


def concise_responses() -> bool:
    """
    Whether an answer carries only the case fields it is about.

    On by default. Off restores the earlier behaviour of returning the whole
    case on every action, which some existing caller may still be relying on.
    """
    return _flag("agent", "concise_responses", True)
