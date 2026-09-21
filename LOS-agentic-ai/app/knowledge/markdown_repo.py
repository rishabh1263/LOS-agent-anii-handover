"""
The corpus, read from markdown files on disk.

Layout:

    knowledge/
      fos/
        document_requirements.md
        address_proof.md
        ...

One directory per stage, one file per topic. Adding knowledge is adding a
markdown file, which is the point: the people who know what a field officer
needs to be told should not have to touch code to say it.

CHUNKED BY HEADING, not by character count. A fixed window cuts a table in
half and splits a rule from its exception, and both halves then retrieve
badly. Markdown already carries the author's own structure; using it means a
retrieved passage is a section somebody wrote as a section.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.knowledge.models import Chunk
from app.knowledge.repository import KnowledgeRepository

logger = logging.getLogger(__name__)

#: A markdown heading: capture level and text.
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$", re.MULTILINE)

#: Headings at or below this level start a new chunk. Deeper headings stay
#: inside the chunk they belong to, so a rule keeps its sub-points.
_SPLIT_LEVEL = 2

#: A section shorter than this is merged into the next one. A two-line
#: heading-plus-sentence retrieves badly on its own and reads as a fragment.
_MIN_CHARS = 120


class MarkdownKnowledgeRepository(KnowledgeRepository):
    """Stage-scoped knowledge from a directory of markdown files."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._cache: dict[str, list[Chunk]] | None = None

    # -- loading ----------------------------------------------------------

    def _load(self) -> dict[str, list[Chunk]]:
        if self._cache is not None:
            return self._cache

        loaded: dict[str, list[Chunk]] = {}

        if not self._root.exists():
            logger.warning("Knowledge root %s does not exist; corpus is empty.",
                           self._root)
            self._cache = loaded
            return loaded

        for stage_dir in sorted(p for p in self._root.iterdir() if p.is_dir()):
            stage = stage_dir.name.upper()
            chunks: list[Chunk] = []

            for path in sorted(stage_dir.glob("*.md")):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError as exc:
                    # One unreadable file must not empty the corpus.
                    logger.warning("Could not read %s: %s", path, exc)
                    continue
                chunks.extend(self._chunk(stage, path.name, text))

            if chunks:
                loaded[stage] = chunks
                logger.info("Knowledge: %s -> %d chunk(s) from %d file(s)",
                            stage, len(chunks), len(list(stage_dir.glob("*.md"))))

        self._cache = loaded
        return loaded

    @staticmethod
    def _chunk(stage: str, source: str, text: str) -> list[Chunk]:
        """Split one file into heading-bounded passages."""
        matches = list(_HEADING.finditer(text))
        if not matches:
            body = text.strip()
            if not body:
                return []
            return [Chunk(chunk_id=f"{stage}:{source}:0", stage=stage,
                          source=source, heading="", text=body)]

        # The document title (the first level-1 heading) labels every chunk
        # that has no nearer heading of its own.
        title = ""
        if matches and len(matches[0].group(1)) == 1:
            title = matches[0].group(2).strip()

        sections: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            level = len(match.group(1))
            heading = match.group(2).strip()

            if level > _SPLIT_LEVEL and sections:
                # Belongs to the section above it; fold it back in.
                previous_heading, previous_body = sections[-1]
                end = (matches[index + 1].start() if index + 1 < len(matches)
                       else len(text))
                sections[-1] = (
                    previous_heading,
                    previous_body + "\n\n" + text[match.start():end].strip(),
                )
                continue

            end = (matches[index + 1].start() if index + 1 < len(matches)
                   else len(text))
            body = text[match.end():end].strip()
            sections.append((heading or title, body))

        # Merge away fragments.
        merged: list[tuple[str, str]] = []
        for heading, body in sections:
            if not body:
                continue
            if merged and len(body) < _MIN_CHARS:
                previous_heading, previous_body = merged[-1]
                merged[-1] = (previous_heading,
                              f"{previous_body}\n\n### {heading}\n{body}")
                continue
            merged.append((heading, body))

        return [
            Chunk(
                chunk_id=f"{stage}:{source}:{index}",
                stage=stage,
                source=source,
                heading=heading,
                text=body,
                metadata={"title": title},
            )
            for index, (heading, body) in enumerate(merged)
        ]

    # -- interface --------------------------------------------------------

    def stages(self) -> list[str]:
        return sorted(self._load())

    def chunks(self, stage: str) -> list[Chunk]:
        return list(self._load().get(str(stage).upper(), []))

    def reload(self) -> None:
        self._cache = None

    def describe(self) -> dict:
        loaded = self._load()
        return {
            "backend": "markdown",
            "root": str(self._root),
            "available": self._root.exists(),
            "stages": {
                stage: {
                    "chunks": len(chunks),
                    "sources": sorted({c.source for c in chunks}),
                }
                for stage, chunks in loaded.items()
            },
        }


__all__ = ["MarkdownKnowledgeRepository"]
