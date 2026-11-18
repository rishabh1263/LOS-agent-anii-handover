"""
The recorded path of one agent run.

    events       every decision the agent reports: node, event, detail
    tool_calls   every tool execution: name, party, status, attempts, time
    model_calls  every model call: purpose, model, status, time, tokens --
                 NEVER the prompt or the completion

REDACTED ON WRITE. Detail passes through `redact` before it is stored: keys
that name a credential are dropped, values that look like a token are
dropped, and PAN / Aadhaar / account numbers are masked by the platform's
disclosure policy (app.security.sensitivity). A trajectory is safe to log,
audit and grade.

DETERMINISTIC REPLAY REPRESENTATION. `replay()` is the run with everything
time-dependent removed (durations, timestamps, run id): two runs over the
same inputs produce the same representation and the same `replay_digest`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

_SECRET_KEY = re.compile(
    r"(authori[sz]ation|token|jwt|secret|password|passwd|api[_-]?key|cookie|credential|"
    r"private[_-]?key|bearer)", re.IGNORECASE)
_SECRET_VALUE = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.|\bBearer\s+\S+|-----BEGIN|\b(sk|pk)-[A-Za-z0-9]{12,}"
    r"|\bAKIA[0-9A-Z]{12,}", re.IGNORECASE)
#: Fields that vary run to run and are excluded from the replay form.
VOLATILE = frozenset({"duration_ms", "elapsed_ms", "started_at", "ended_at", "timestamp",
                      "run_id", "fetched_at", "observed_at", "created_at", "updated_at"})
REDACTED = "[redacted]"


def redact(value: Any, *, _key: str | None = None) -> Any:
    """Credentials removed, identifiers masked. Structure preserved."""
    if _key is not None and _SECRET_KEY.search(_key):
        return REDACTED
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return REDACTED
        from app.security.sensitivity import mask_identifiers

        return mask_identifiers(value)
    if isinstance(value, dict):
        return {str(k): redact(v, _key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in sorted(value.items()) if k not in VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


@dataclass
class ToolCallRecord:
    tool: str
    party_id: str | None
    status: str
    attempts: int
    duration_ms: float
    error: str | None = None
    step: str | None = None


@dataclass
class ModelCallRecord:
    purpose: str
    model: str | None
    status: str                 # OK | TIMEOUT | ERROR | REFUSED | SKIPPED
    duration_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


@dataclass
class Trajectory:
    agent_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    model_calls: list[ModelCallRecord] = field(default_factory=list)

    def record(self, node: str, event: str, **detail: Any) -> dict[str, Any]:
        entry = {"seq": len(self.events) + 1, "node": node, "event": event,
                 **{k: v for k, v in redact(detail).items() if v is not None}}
        self.events.append(entry)
        return entry

    def record_tool(self, record: ToolCallRecord) -> None:
        self.tool_calls.append(record)

    def record_model(self, record: ModelCallRecord) -> None:
        self.model_calls.append(record)

    # -- views ---------------------------------------------------------------
    def tools_used(self) -> list[tuple[str, str | None]]:
        return [(t.tool, t.party_id) for t in self.tool_calls]

    def as_dict(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "events": list(self.events),
                "tool_calls": [asdict(t) for t in self.tool_calls],
                "model_calls": [asdict(m) for m in self.model_calls]}

    def replay(self) -> dict[str, Any]:
        """Time-independent form: equal for equal inputs."""
        return _stable(self.as_dict())

    def replay_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.replay(), sort_keys=True, default=str)
                              .encode()).hexdigest()


__all__ = ["ModelCallRecord", "REDACTED", "ToolCallRecord", "Trajectory", "VOLATILE", "redact"]
