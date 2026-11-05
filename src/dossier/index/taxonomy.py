"""``dsr taxonomy suggest``: propose a controlled topic vocabulary from free tags.

Without a taxonomy, cards carry free-form topics. This command counts them and
(when the index model is available) asks it to merge synonyms into at most 15
topics. The result is written as a TOML snippet for a person to review and paste
into ``dossier.toml``; nothing is applied automatically.
"""

from __future__ import annotations

import re
from collections import Counter

from pydantic import BaseModel, Field

from dossier.core.errors import ProviderUnavailableError
from dossier.core.io import atomic_write_text
from dossier.core.workspace import Workspace
from dossier.index.pipeline import load_manifest
from dossier.llm.router import ModelRouter
from dossier.llm.structured import Outcome, generate_structured


class _Topic(BaseModel):
    key: str = Field(max_length=40)
    label: str = Field(max_length=60)


class _Taxonomy(BaseModel):
    topics: list[_Topic] = Field(max_length=15)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "topic"


def suggest(workspace: Workspace) -> tuple[list[tuple[str, str]], str]:
    manifest = load_manifest(workspace.paths)
    counts = Counter(topic for doc in manifest.docs.values() for topic in doc.topics)
    fallback = [(_slug(topic), topic) for topic, _ in counts.most_common(15)]
    method = "most frequent tags"
    topics = fallback
    router = ModelRouter(workspace.config)
    if counts and router.has("index"):
        listing = "\n".join(f"{topic} ({n})" for topic, n in counts.most_common(80))
        try:
            result = generate_structured(
                router.client("index"),
                system="You organize document tags into a short, clear topic list.",
                user=(
                    f"Tags with document counts:\n{listing}\n\nMerge synonyms and near-duplicates "
                    "into at most 15 topics. Return topics, each with key (lowercase-with-dashes) "
                    "and label (2-4 words)."
                ),
                schema=_Taxonomy,
                fallback=lambda: _Taxonomy(topics=[]),
            )
            if result.outcome is not Outcome.DEGRADED and result.value.topics:
                topics = [(_slug(t.key), t.label.strip()) for t in result.value.topics]
                method = f"merged by {router.route('index').model}"
        except ProviderUnavailableError:
            pass
    unique = list(dict.fromkeys(topics))
    lines = [
        f"# Suggested taxonomy ({method}). Review, edit, then paste into dossier.toml.",
        "[taxonomy]",
        "topics = [",
        *[f'  {{ key = "{key}", label = "{label}" }},' for key, label in unique],
        "]",
    ]
    snippet = "\n".join(lines) + "\n"
    atomic_write_text(workspace.paths.index / "taxonomy-suggestion.toml", snippet)
    return unique, snippet
