"""The English gate (evals/copilot/english_gate.py) measures only while its held-out set stays frozen and unseen."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: held-out English questions that ALSO sit, word for word, in a config VALUE (found 2026-10-28, all older than the
#: gate). Common phrasings, but the held-out score of those turns is not blind: no new ones may be added.
KNOWN_OVERLAPS = {"can you check it", "why is this case stuck"}


def _values(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _values(v)
    elif isinstance(node, str):
        yield node.strip().lower()


def test_the_english_held_out_set_is_still_frozen():
    from evals.copilot import english_gate

    held = ROOT / "evals" / "copilot" / "agent_heldout_en.yaml"
    assert hashlib.md5(held.read_bytes()).hexdigest() == english_gate.FROZEN_MD5, \
        "agent_heldout_en.yaml was edited: a held-out set is never tuned on -- restore it, add new cases elsewhere"


def test_no_new_held_out_sentence_is_copied_into_the_config():
    import yaml

    held = yaml.safe_load((ROOT / "evals" / "copilot" / "agent_heldout_en.yaml").read_text(encoding="utf-8"))
    questions = {t["q"].strip().lower().rstrip("?") for c in held["conversations"] for t in c["turns"]}
    values = set()
    for path in (ROOT / "app" / "config").rglob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        values.update(v.rstrip("?") for v in _values(data))       # config VALUES, never comments
    copied = sorted(q for q in questions if len(q) > 12 and q in values)
    assert set(copied) <= KNOWN_OVERLAPS, sorted(set(copied) - KNOWN_OVERLAPS)
