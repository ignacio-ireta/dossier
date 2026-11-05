"""Document cards: what a document is about, its key points, and where things are.

Built for small models. A document that fits the ``index`` role's input budget
gets one call; a longer one is mapped window by window (each call summarizes
one window) and then reduced from those window summaries. Every call asks for a
tiny flat JSON object. When a call cannot produce valid output, a deterministic
card is used (first sentences, headings, frequent terms) and marked
``degraded`` so it is regenerated on a later run.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from dossier.core.config import WorkspaceConfig
from dossier.index.chunking import Chunk, OutlineEntry, chunk_markdown
from dossier.llm.budget import estimate_tokens, pack, truncate_to_tokens
from dossier.llm.router import RoleClient
from dossier.llm.structured import Outcome, generate_structured

CARD_VERSION = 1
_PROMPT_OVERHEAD_TOKENS = 700
_WORD = re.compile(r"[^\W\d_][\w'-]{2,}", re.UNICODE)
_STOPWORDS = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "was",
        "were",
        "will",
        "have",
        "has",
        "had",
        "not",
        "but",
        "you",
        "your",
        "our",
        "their",
        "they",
        "them",
        "its",
        "into",
        "than",
        "then",
        "there",
        "here",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "how",
        "all",
        "any",
        "each",
        "other",
        "some",
        "such",
        "only",
        "own",
        "same",
        "can",
        "could",
        "should",
        "would",
        "may",
        "might",
        "must",
        "shall",
        "about",
        "above",
        "after",
        "again",
        "against",
        "below",
        "between",
        "both",
        "during",
        "through",
        "under",
        "until",
        "while",
        "also",
        "el",
        "la",
        "los",
        "las",
        "del",
        "que",
        "con",
        "por",
        "para",
        "una",
        "uno",
        "unos",
        "unas",
        "como",
        "más",
        "pero",
        "sus",
        "este",
        "esta",
        "estos",
        "page",
        "slide",
        "sheet",
        "data",
        "rows",
        "columns",
        "source",
        "file",
        "image",
    ]
)


class _Card(BaseModel):
    summary: str = Field(max_length=900, description="2 to 4 factual sentences.")
    key_points: list[str] = Field(default_factory=list, max_length=6)
    topics: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("key_points", "topics")
    @classmethod
    def _strip(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


class _Part(BaseModel):
    summary: str = Field(max_length=600, description="1 to 3 factual sentences.")
    key_points: list[str] = Field(default_factory=list, max_length=4)


@dataclass
class CardContent:
    title: str
    summary: str
    key_points: list[str]
    topics: list[str]
    status: str  # ok | degraded
    outline: list[OutlineEntry] = field(default_factory=list)
    calls: int = 0
    seconds: float = 0.0

    @property
    def one_line(self) -> str:
        first = re.split(r"(?<=[.!?])\s", self.summary.strip(), maxsplit=1)[0]
        return first[:180]


def _system(config: WorkspaceConfig) -> str:
    brief = config.workspace.brief.strip()
    context = f" Workspace context: {brief}" if brief else ""
    return (
        f'You summarize documents for the workspace "{config.workspace.name}".{context} '
        "Use only facts stated in the text. Keep names, numbers and dates exactly as written. "
        "Do not guess missing facts. Write in the document's language."
    )


def _topic_rule(candidates: list[str] | None) -> str:
    if candidates:
        return "topics: choose up to 4 from this list only: " + ", ".join(candidates) + "."
    return "topics: up to 5 short lowercase keywords (e.g. budget, data platform)."


def keywords(text: str, limit: int = 5) -> list[str]:
    counts = Counter(word.lower() for word in _WORD.findall(text) if word.lower() not in _STOPWORDS)
    return [word for word, _ in counts.most_common(limit)]


def fallback_card(title: str, chunks: list[Chunk], outline: list[OutlineEntry]) -> CardContent:
    body = " ".join(chunk.text for chunk in chunks)
    plain = re.sub(r"[|#*`>\-]+", " ", body)
    plain = re.sub(r"\s+", " ", plain).strip()
    sentences = re.split(r"(?<=[.!?])\s", plain)
    summary = " ".join(sentences[:3])[:500] or f"{title} (no text extracted)."
    points = [entry.text for entry in outline if entry.level >= 2][:5]
    return CardContent(title, summary, points, keywords(plain), "degraded", outline)


def _document_text(chunks: list[Chunk]) -> list[str]:
    return [f"[{c.heading}]\n{c.text}" if c.heading else c.text for c in chunks]


def build_card(
    client: RoleClient | None,
    config: WorkspaceConfig,
    *,
    title: str,
    markdown: str,
    outline: list[OutlineEntry],
    topic_candidates: list[str] | None = None,
    max_windows: int = 24,
) -> CardContent:
    chunks = chunk_markdown(markdown, max_tokens=350)
    if client is None or not chunks:
        content = fallback_card(title, chunks, outline)
        if not chunks:
            content.status = "ok"  # nothing to summarize is not a model failure
        return content
    system = _system(config)
    budget = max(400, client.input_budget_tokens - _PROMPT_OVERHEAD_TOKENS)
    pieces = _document_text(chunks)
    full = "\n\n".join(pieces)
    calls = 0
    seconds = 0.0

    def fallback() -> _Card:
        basic = fallback_card(title, chunks, outline)
        return _Card(summary=basic.summary, key_points=basic.key_points, topics=basic.topics)

    if estimate_tokens(full) <= budget:
        result = generate_structured(
            client,
            system=system,
            user=(
                f'Document: "{title}"\n\n{full}\n\nReturn: summary (2-4 sentences: what the '
                "document is and its main facts), key_points (up to 5 short facts with exact "
                f"figures), {_topic_rule(topic_candidates)}"
            ),
            schema=_Card,
            fallback=fallback,
        )
        calls, seconds = result.attempts, result.seconds
        card = result.value
        degraded = result.outcome is Outcome.DEGRADED
    else:
        windows = pack(pieces, budget)[:max_windows]
        parts: list[str] = []
        degraded = False
        for number, window in enumerate(windows, start=1):
            text = "\n\n".join(pieces[i] for i in window)
            part = generate_structured(
                client,
                system=system,
                user=(
                    f'Part {number} of {len(windows)} of "{title}":\n\n{text}\n\nReturn: summary '
                    "(1-3 sentences on this part), key_points (up to 3 exact facts)."
                ),
                schema=_Part,
                fallback=lambda: _Part(summary=""),
            )
            calls += part.attempts
            seconds += part.seconds
            if part.outcome is Outcome.DEGRADED or not part.value.summary:
                degraded = True
                continue
            points = "; ".join(part.value.key_points)
            parts.append(
                f"Part {number}: {part.value.summary}" + (f" Facts: {points}" if points else "")
            )
        notes = truncate_to_tokens("\n".join(parts), budget)
        if not notes.strip():
            card = fallback()
            degraded = True
        else:
            result = generate_structured(
                client,
                system=system,
                user=(
                    f'Notes on consecutive parts of "{title}":\n\n{notes}\n\nWrite the card for '
                    "the whole document. Return: summary (2-4 sentences), key_points (up to 5 "
                    f"exact facts), {_topic_rule(topic_candidates)}"
                ),
                schema=_Card,
                fallback=fallback,
            )
            calls += result.attempts
            seconds += result.seconds
            card = result.value
            degraded = degraded or result.outcome is Outcome.DEGRADED
    topics = [t.lower().strip() for t in card.topics]
    if topic_candidates:
        allowed = set(topic_candidates)
        topics = [t for t in topics if t in allowed]
    return CardContent(
        title=title,
        summary=card.summary.strip(),
        key_points=card.key_points[:5],
        topics=list(dict.fromkeys(topics))[:5],
        status="degraded" if degraded else "ok",
        outline=outline,
        calls=calls,
        seconds=round(seconds, 2),
    )


def render_card(
    card: CardContent,
    *,
    doc_key: str,
    md_path: str,
    doc_type: str,
    words: int,
    authority: str | None,
) -> str:
    import json

    front = {
        "doc": doc_key,
        "corpus": md_path,
        "title": card.title,
        "type": doc_type,
        "status": card.status,
        "topics": card.topics,
        "authority": authority,
        "words": words,
        "card_version": CARD_VERSION,
    }
    lines = ["---"]
    lines += [
        f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items() if v is not None
    ]
    lines += ["---", "", f"# {card.title}", "", card.summary, ""]
    if card.status == "degraded":
        lines += ["> Generated without a model (fallback); it will be refreshed.", ""]
    if card.key_points:
        lines += ["## Key points", ""] + [f"- {point}" for point in card.key_points] + [""]
    if card.outline:
        lines += ["## Where to find what", ""]
        lines += [
            f"- line {entry.line}: {'  ' * max(0, entry.level - 2)}{entry.text}"
            for entry in card.outline
            if entry.level >= 2
        ][:60]
        lines += ["", f"Open with `dsr read {doc_key} --lines <from>-<to>`.", ""]
    if card.topics:
        lines += ["## Topics", "", ", ".join(card.topics), ""]
    return "\n".join(lines).rstrip() + "\n"
