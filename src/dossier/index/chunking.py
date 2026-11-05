"""Structure-aware chunking of converted Markdown.

Chunks follow the document's own structure: a heading starts a new chunk, and
paragraphs are packed until the token budget. Each chunk keeps its heading path
("Slide 2: Phases > Risks") and the line range it came from, so a search hit can
be opened at the exact place with ``dsr read --lines``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dossier.llm.budget import estimate_tokens, split_text

CHUNKER_VERSION = 1
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^(```|~~~)")
_SKIP_SECTIONS = {"extraction warnings"}


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading: str
    text: str
    line_start: int  # 1-based, in the Markdown file
    line_end: int


@dataclass(frozen=True)
class OutlineEntry:
    level: int
    text: str
    line: int


def split_front_matter(text: str) -> tuple[dict[str, str], list[str], int]:
    """Front-matter fields, body lines, and how many lines the front matter used."""
    lines = text.splitlines()
    fields: dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                for raw in lines[1:index]:
                    key, _, value = raw.partition(":")
                    fields[key.strip()] = value.strip().strip('"')
                return fields, lines[index + 1 :], index + 1
    return fields, lines, 0


def outline(text: str) -> list[OutlineEntry]:
    _, body, offset = split_front_matter(text)
    entries: list[OutlineEntry] = []
    in_fence = False
    for number, line in enumerate(body, start=offset + 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADING.match(line)
        if match:
            entries.append(OutlineEntry(len(match.group(1)), match.group(2), number))
    return entries


def _blocks(text: str) -> list[tuple[list[str], str, int, int]]:
    """(heading path, paragraph text, first line, last line) for each paragraph."""
    _, body, offset = split_front_matter(text)
    stack: list[tuple[int, str]] = []
    blocks: list[tuple[list[str], str, int, int]] = []
    current: list[str] = []
    start = 0
    in_fence = False
    skipping = False

    def flush(end: int) -> None:
        nonlocal current
        if current and not skipping:
            path = [title for level, title in stack if level > 1]
            blocks.append((path, "\n".join(current).strip(), start, end))
        current = []

    for number, line in enumerate(body, start=offset + 1):
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            flush(number - 1)
            level, title = len(heading.group(1)), heading.group(2)
            stack = [(lvl, t) for lvl, t in stack if lvl < level] + [(level, title)]
            skipping = level == 2 and title.strip().lower() in _SKIP_SECTIONS
            continue
        if not line.strip() and not in_fence:
            flush(number - 1)
            continue
        if not current:
            start = number
        current.append(line)
    flush(offset + len(body))
    return blocks


def chunk_markdown(text: str, *, max_tokens: int = 350) -> list[Chunk]:
    chunks: list[Chunk] = []
    pending: list[str] = []
    pending_heading = ""
    pending_start = pending_end = 0

    def emit() -> None:
        nonlocal pending
        if pending:
            chunks.append(
                Chunk(
                    len(chunks), pending_heading, "\n\n".join(pending), pending_start, pending_end
                )
            )
        pending = []

    for path, paragraph, first, last in _blocks(text):
        heading = " > ".join(path)
        pieces = (
            split_text(paragraph, max_tokens)
            if estimate_tokens(paragraph) > max_tokens
            else [paragraph]
        )
        for piece in pieces:
            same_section = heading == pending_heading
            fits = estimate_tokens("\n\n".join([*pending, piece])) <= max_tokens
            if pending and (not same_section or not fits):
                emit()
            if not pending:
                pending_heading, pending_start = heading, first
            pending.append(piece)
            pending_end = last
    emit()
    return chunks
