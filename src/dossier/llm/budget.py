"""Token budgets for small context windows.

Estimates are deliberately conservative (characters / 3.2) so mixed-language
text, code and tables still fit; being a little under budget costs nothing,
overflowing a 8K window truncates silently on some servers.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

CHARS_PER_TOKEN = 3.2
TRUNCATION_MARK = "\n[…truncated]"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def chars_for_tokens(tokens: int) -> int:
    return int(tokens * CHARS_PER_TOKEN)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Cut at a paragraph, line or word boundary near the limit."""
    limit = chars_for_tokens(max_tokens)
    if len(text) <= limit:
        return text
    head = text[: max(0, limit - len(TRUNCATION_MARK))]
    for boundary in ("\n\n", "\n", " "):
        cut = head.rfind(boundary)
        if cut >= len(head) * 0.6:
            head = head[:cut]
            break
    return head.rstrip() + TRUNCATION_MARK


_PARAGRAPHS = re.compile(r"\n\s*\n")


def split_text(text: str, max_tokens: int) -> list[str]:
    """Split text into pieces of at most ``max_tokens``, preferring paragraph breaks."""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    limit = chars_for_tokens(max_tokens)
    pieces: list[str] = []
    current = ""
    for paragraph in _PARAGRAPHS.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > limit:
            cut = paragraph.rfind(" ", 0, limit)
            cut = cut if cut > limit * 0.5 else limit
            if current:
                pieces.append(current)
                current = ""
            pieces.append(paragraph[:cut].rstrip())
            paragraph = paragraph[cut:].lstrip()
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > limit:
            pieces.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def pack(pieces: Sequence[str], max_tokens: int, *, separator: str = "\n\n") -> list[list[int]]:
    """Group consecutive pieces into windows that fit ``max_tokens``.

    Returns lists of piece indices. A piece larger than the budget gets a window
    of its own; callers should split such pieces first with :func:`split_text`.
    """
    windows: list[list[int]] = []
    current: list[int] = []
    used = 0
    sep_tokens = estimate_tokens(separator)
    for index, piece in enumerate(pieces):
        cost = estimate_tokens(piece)
        extra = cost + (sep_tokens if current else 0)
        if current and used + extra > max_tokens:
            windows.append(current)
            current, used = [], 0
            extra = cost
        current.append(index)
        used += extra
    if current:
        windows.append(current)
    return windows
