"""A scripted provider for tests and offline dry runs."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dossier.core.errors import DossierError
from dossier.llm.types import ChatResponse, JsonSchema, Message, RerankResult

Responder = Callable[[list[Message], JsonSchema | None], str]


def hash_embedding(text: str, dimensions: int = 16) -> list[float]:
    """Deterministic unit vector from a text's words; similar texts share words."""
    vector = [0.0] * dimensions
    for word in text.lower().split():
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        vector[digest[0] % dimensions] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


@dataclass
class FakeProvider:
    """Replies come from ``script`` (in order) or from ``responder``.

    A script item that is an exception instance is raised instead of returned.
    """

    name: str = "fake"
    script: list[str | DossierError] = field(default_factory=list)
    responder: Responder | None = None
    models: list[str] = field(default_factory=lambda: ["fake-model"])
    calls: list[dict[str, Any]] = field(default_factory=list)
    dimensions: int = 16

    def chat(
        self,
        model: str,
        messages: list[Message],
        *,
        json_schema: JsonSchema | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        context_tokens: int | None = None,
        think: bool | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResponse:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "json_schema": json_schema,
                "max_tokens": max_tokens,
                "think": think,
            }
        )
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, DossierError):
                raise item
            return ChatResponse(text=item, model=model)
        if self.responder is not None:
            return ChatResponse(text=self.responder(messages, json_schema), model=model)
        return ChatResponse(text="{}", model=model)

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        del model
        return [hash_embedding(text, self.dimensions) for text in texts]

    def rerank(self, model: str, query: str, documents: list[str]) -> list[RerankResult]:
        del model
        q = set(query.lower().split())
        scored = [
            RerankResult(i, len(q & set(doc.lower().split())) / (len(q) or 1))
            for i, doc in enumerate(documents)
        ]
        return sorted(scored, key=lambda r: r.score, reverse=True)

    def list_models(self) -> list[str]:
        return list(self.models)
