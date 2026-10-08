"""
PLAUSIBLE AMOUNTS AT INTAKE (FOS plan 1.1, production protection).

Every amount an application records is in RUPEES. A figure below its configured minimum ("5" for a
home loan -- almost always lakhs typed as rupees) or written with a unit word ("5 lakh", "2 cr") is
refused at creation / update with one clear message, so it never reaches the case. The same config
(applicant_agent.yaml chatbot.plausibility) keeps the chatbot from ever saying such a figure.
"""

from __future__ import annotations

import re
from typing import Any

CODE = "AMOUNT_IMPLAUSIBLE"


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("plausibility") or {}


def problem(values: dict[str, Any]) -> tuple[str, str] | None:
    """(field, message) for the first implausible amount among `values`, else None. Absent / blank = fine."""
    cfg = _cfg()
    minimum = cfg.get("minimum") or {}
    units = [str(u).lower() for u in cfg.get("unit_words") or []]
    examples = cfg.get("examples") or {}
    labels = cfg.get("field_labels") or {}
    for field, floor in minimum.items():
        raw = values.get(field)
        if raw is None or not str(raw).strip():
            continue
        text = str(raw).strip().lower()
        label = str(labels.get(field) or field.replace("_", " ").capitalize())
        example = examples.get(field, 500000)
        # a unit right after the number or after a space: "5 lakh", "50k", "2cr"
        if any(re.search(rf"(?:^|[\d\s.]){re.escape(u)}\b", text) for u in units):
            return field, str(cfg.get("unit_message") or "{label}: enter the full amount in rupees, e.g. {example}.") \
                .format(label=label, example=example)
        try:
            amount = float(re.sub(r"[,\s₹]|rs\.?|inr", "", text))
        except ValueError:
            continue                                   # not a number: the existing schema rules decide
        if amount < float(floor):
            return field, str(cfg.get("reject_message")
                              or "{label} looks too small; enter the full amount in rupees, e.g. {example}.") \
                .format(label=label, example=example)
    return None


__all__ = ["CODE", "problem"]
