from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant"]
JsonSchema = dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    images: tuple[bytes, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ChatResponse:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    seconds: float = 0.0
    # True: the server enforced the JSON schema; False: it could not and the
    # reply was only prompted; None: no schema was requested.
    constrained: bool | None = None


@dataclass(frozen=True)
class RerankResult:
    index: int
    score: float


class Provider(Protocol):
    name: str

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
    ) -> ChatResponse: ...

    def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...

    def rerank(self, model: str, query: str, documents: list[str]) -> list[RerankResult]: ...

    def list_models(self) -> list[str]: ...
