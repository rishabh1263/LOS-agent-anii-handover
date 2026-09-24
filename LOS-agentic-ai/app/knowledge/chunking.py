"""
Splitting text into pieces small enough to embed and large enough to mean
something.

TOKENS ARE COUNTED AS WHITESPACE WORDS, DELIBERATELY. A real tokenizer
would be a new dependency, a model-specific one, and a source of drift
the moment the embedding model changed -- and this layer does not need
that precision. A word count is within a predictable factor of any
sub-word tokenizer for LOS prose, it is deterministic, and it costs
nothing. The defaults are expressed in that unit and the docstring says
so, rather than claiming an accuracy the implementation does not have.

SHORT SOURCES STAY WHOLE. Most derived LOS text is one or two
sentences -- a reason code explained, a stage entered. Padding those
into 600-token blocks would bury the sentence that matters among five
unrelated ones, and every chunk would retrieve for every query.

EVERY CHUNK CARRIES ITS PROVENANCE. A chunk that cannot say which
finding, document, case and applicant it came from is a chunk a
reviewer cannot check, and this pipeline exists to produce evidence
rather than fluent text.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

#: Environment-driven, because the right size depends on the embedding
#: model and the corpus, and neither is decided in this file.
ENV_CHUNK_SIZE = "LOS_CHUNK_SIZE"
ENV_CHUNK_OVERLAP = "LOS_CHUNK_OVERLAP"

DEFAULT_CHUNK_SIZE = 600
DEFAULT_OVERLAP = 100


def _positive_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def chunk_size() -> int:
    return _positive_int(ENV_CHUNK_SIZE, DEFAULT_CHUNK_SIZE)


def chunk_overlap() -> int:
    """
    How much each chunk repeats of the one before.

    CLAMPED BELOW THE CHUNK SIZE. An overlap greater than or equal to
    the window would advance the cursor by zero or less and loop
    forever -- a configuration mistake that must not be able to hang
    an indexing run.
    """
    size = chunk_size()
    overlap = _positive_int(ENV_CHUNK_OVERLAP, DEFAULT_OVERLAP)
    return min(overlap, max(0, size - 1))


@dataclass(frozen=True)
class Chunk:
    """One piece of text, and where it came from."""

    text: str
    index: int
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return len(self.text.split())


def split(text: str, *, size: int | None = None,
          overlap: int | None = None) -> list[str]:
    """
    One string, in windows of `size` words overlapping by `overlap`.

    Returns [] for empty input rather than [""] -- an empty chunk
    would be embedded, stored and retrieved, and it means nothing.
    """
    words = (text or "").split()
    if not words:
        return []

    window = int(size or chunk_size())
    step_back = int(overlap if overlap is not None else chunk_overlap())
    step_back = min(step_back, max(0, window - 1))

    if len(words) <= window:
        return [" ".join(words)]

    out: list[str] = []
    start = 0
    while start < len(words):
        out.append(" ".join(words[start:start + window]))
        if start + window >= len(words):
            break
        start += window - step_back

    return out


def chunk(text: str, payload: dict[str, Any], *, size: int | None = None,
          overlap: int | None = None) -> list[Chunk]:
    """
    One derived text, chunked, each piece carrying the same provenance.

    `chunk_index` is added to every payload so a reader can order the
    pieces of one source and so the point id is stable.
    """
    pieces = split(text, size=size, overlap=overlap)

    return [
        Chunk(text=piece, index=position,
              payload={**payload, "chunk_index": position, "text": piece})
        for position, piece in enumerate(pieces)
    ]


def point_id(source_key: str, index: int) -> str:
    """
    A chunk's stable identity.

    DERIVED FROM THE SOURCE, NOT GENERATED. Re-indexing the same
    finding must replace its chunk rather than add a second copy
    beside it, so the id is a function of what the chunk is OF --
    never of when it was written.
    """
    return f"{source_key}#{int(index)}"


__all__ = [
    "Chunk", "DEFAULT_CHUNK_SIZE", "DEFAULT_OVERLAP", "ENV_CHUNK_OVERLAP",
    "ENV_CHUNK_SIZE", "chunk", "chunk_overlap", "chunk_size", "point_id",
    "split",
]
