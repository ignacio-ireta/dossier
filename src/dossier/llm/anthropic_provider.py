"""Optional Anthropic provider (``cloud`` extra).

Always external under the workspace policy: it is only reachable when
``allow_external`` is true and the provider is listed in ``allowed_providers``.

Request shape follows the current Messages API: structured output through
``output_config.format`` (no ``temperature``; thinking left at the model's
default). The schema is trimmed to what structured outputs accept; dossier
re-validates every reply with pydantic, so dropped constraints such as
``maxLength`` are still enforced.
"""

from __future__ import annotations

import base64
import copy
import time
from typing import Any

from dossier.core.errors import (
    CapabilityUnavailableError,
    ConfigError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from dossier.llm.types import ChatResponse, JsonSchema, Message, RerankResult

_UNSUPPORTED_KEYS = {
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minItems",
    "maxItems",
    "uniqueItems",
    "pattern",
    "default",
    "title",
}


def to_structured_output_schema(schema: JsonSchema) -> JsonSchema:
    """Drop unsupported constraints; close every object (``additionalProperties: false``)."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            cleaned = {k: walk(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYS}
            if cleaned.get("type") == "object" or "properties" in cleaned:
                cleaned["additionalProperties"] = False
            return cleaned
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    result: JsonSchema = walk(copy.deepcopy(schema))
    return result


def _media_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


class AnthropicProvider:
    def __init__(self, name: str, *, api_key: str | None, timeout_s: float) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ConfigError(
                "the anthropic provider needs the cloud extra: uv tool install 'dossier[cloud]'"
            ) from exc
        if not api_key:
            raise ConfigError(
                f"provider {name!r}: no API key; store one with `dsr secrets set <name>` "
                "and set api_key_secret"
            )
        self.name = name
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s)

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
        # Sampling parameters are not accepted by current models, and thinking is
        # left at the model default; effort can be set through ``extra``.
        del temperature, context_tokens, think
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        wire: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            content: list[dict[str, Any]] = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _media_type(image),
                        "data": base64.standard_b64encode(image).decode("ascii"),
                    },
                }
                for image in m.images
            ]
            content.append({"type": "text", "text": m.content})
            wire.append({"role": m.role, "content": content})
        output_config: dict[str, Any] = dict((extra or {}).get("output_config", {}))
        if json_schema is not None:
            output_config["format"] = {
                "type": "json_schema",
                "schema": to_structured_output_schema(json_schema),
            }
        params: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": wire}
        if system:
            params["system"] = system
        if output_config:
            params["output_config"] = output_config
        start = time.monotonic()
        errors = self._anthropic
        try:
            response = self._client.messages.create(**params)
        except errors.APIConnectionError as exc:  # includes timeouts
            raise ProviderUnavailableError(f"{self.name}: {type(exc).__name__}") from exc
        except errors.APIStatusError as exc:
            raise ProviderResponseError(
                f"{self.name}: HTTP {exc.status_code} ({type(exc).__name__}), "
                f"request {getattr(exc, 'request_id', None) or 'unknown'}"
            ) from exc
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ProviderResponseError(f"{self.name}: request declined ({category or 'refusal'})")
        if response.stop_reason == "max_tokens":
            raise ProviderResponseError(
                f"{self.name}: output hit max_tokens={max_tokens}; raise max_output_tokens "
                "for this role (thinking counts toward it)"
            )
        text = "".join(block.text for block in response.content if block.type == "text")
        return ChatResponse(
            text=text,
            model=model,
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            seconds=round(time.monotonic() - start, 3),
            constrained=True if json_schema is not None else None,
        )

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        del model, texts
        raise CapabilityUnavailableError(f"{self.name}: Anthropic has no embeddings API")

    def rerank(self, model: str, query: str, documents: list[str]) -> list[RerankResult]:
        del model, query, documents
        raise CapabilityUnavailableError(f"{self.name}: Anthropic has no rerank API")

    def list_models(self) -> list[str]:
        try:
            return sorted(model.id for model in self._client.models.list())
        except self._anthropic.APIConnectionError as exc:
            raise ProviderUnavailableError(f"{self.name}: {type(exc).__name__}") from exc
        except self._anthropic.APIStatusError as exc:
            raise ProviderResponseError(f"{self.name}: HTTP {exc.status_code}") from exc
