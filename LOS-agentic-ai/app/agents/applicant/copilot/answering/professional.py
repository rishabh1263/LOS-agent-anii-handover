"""
PROFESSIONAL FORMAT (FOS plan section 3; flag COPILOT_PROFESSIONAL_FORMAT, config chatbot.professional_format --
ON in dev, the production default). Applied LAST to every reply of BOTH endpoints (/fos/copilot, /copilot/query):

    * no emojis anywhere: answer, answer_markdown, answer_plain, suggested_questions, every label / header / button
      string inside presentation, actions and clarification options (`emojis: false`);
    * the closing hint ("👉 Upload these ...") becomes one "Next step:" line (`next_step_label`);
    * `answer_plain` is always present: the answer with no markdown markers.

Facts are never touched: only pictographs, the hint marker and markdown markers are removed.
"""

from __future__ import annotations

import os
import re
from typing import Any

FLAG = "COPILOT_PROFESSIONAL_FORMAT"
_ON = {"1", "true", "yes", "on"}

#: pictographs, dingbats, symbols, regional flags, variation selectors and joiners -- never letters or digits
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF"
    "⌀-⏿️‍⃣ℹ]")      # (arrows such as "FOS → CPA" are text, kept)
_MARKDOWN = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")


def _cfg() -> dict[str, Any]:
    from app.agents.applicant import config

    return config.chatbot("professional_format")


def enabled() -> bool:
    value = os.getenv(FLAG)
    if value is not None and value.strip():
        return value.strip().lower() in _ON
    return bool(_cfg().get("enabled", False))


def strip_emojis(text: str) -> str:
    """The text without pictographs; the spaces they leave are collapsed per line, line breaks kept."""
    if not isinstance(text, str) or not text:
        return text
    lines = []
    for line in text.split("\n"):
        indent = re.match(r"^[ \t]*", line).group(0)          # the line's own indent, not the gap an emoji leaves
        lines.append(indent + " ".join(_EMOJI.sub("", line).split()))
    return "\n".join(lines).strip("\n")


def _bulleted(text: str) -> str:
    """Point-wise: a row that LED with a pictograph ("📄 PAN [Upload]") becomes a "- " bullet; the first line (the
    direct answer / case header) and a heading ending with ":" stay plain."""
    lines = str(text or "").split("\n")
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if i == 0 or not stripped or not _EMOJI.match(stripped):
            continue
        rest = _EMOJI.sub("", stripped).strip()
        if rest and not rest.endswith(":") and not rest.startswith(("-", "•")):
            lines[i] = line[:len(line) - len(stripped)] + "- " + rest
    return "\n".join(lines)


def plain(text: str) -> str:
    """The text without markdown markers (bold / underline)."""
    return _MARKDOWN.sub(lambda m: m.group(1) or m.group(2), str(text or ""))


def _next_step(text: str) -> str:
    """The closing "👉 ..." hint as one "Next step:" line (other 👉 lines simply lose the marker)."""
    label = str(_cfg().get("next_step_label") or "Next step:")
    lines = str(text or "").rstrip().split("\n")
    if lines and lines[-1].lstrip().startswith("👉"):
        body = lines[-1].lstrip()[1:].strip()
        lines[-1] = f"**{label}** {body}" if body else ""
    return "\n".join(lines)


def _walk(value: Any, emojis: bool) -> Any:
    """Every string inside: a pictograph is never a fact, so it goes from icons, labels and texts alike."""
    if isinstance(value, str):
        return value if emojis else strip_emojis(value)
    if isinstance(value, dict):
        return {k: _walk(v, emojis) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, emojis) for v in value]
    return value


def apply(reply: dict[str, Any]) -> dict[str, Any]:
    """The reply in the professional format (a copy; facts unchanged)."""
    if not isinstance(reply, dict) or not enabled():
        return reply
    emojis = bool(_cfg().get("emojis", False))
    out = dict(reply)
    for key in ("answer", "answer_markdown"):
        if isinstance(out.get(key), str):
            text = _next_step(out[key])
            out[key] = text if emojis else strip_emojis(_bulleted(text))
    for key in ("presentation", "actions", "clarification_required", "workspace_view", "document_actions",
                "suggested_questions", "options"):
        if key in out:
            out[key] = _walk(out[key], emojis)
    if isinstance(out.get("answer"), str):
        out["answer_plain"] = plain(out["answer"])
    out["format"] = {"style": "PROFESSIONAL", "emojis": emojis}
    return out


__all__ = ["FLAG", "apply", "enabled", "plain", "strip_emojis"]
