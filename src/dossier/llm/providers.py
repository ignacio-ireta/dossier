"""Model providers.

All local runtimes dossier targets (Ollama, llama.cpp ``llama-server``, LM
Studio, vLLM, MLX servers) speak either Ollama's native API or the OpenAI chat
API, so two HTTP clients cover them. Structured output uses each server's
constrained decoding: Ollama ``format`` and OpenAI ``response_format``.

The optional Anthropic adapter lives in ``anthropic_provider`` and is imported
only when a workspace configures it.

Errors are split so callers can react correctly:
- :class:`ProviderUnavailableError`: nothing answered (server down, timeout).
- :class:`ProviderResponseError`: the server answered with an error.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import httpx

from dossier.core.config import ProviderConfig, ProviderKind
from dossier.core.errors import (
    CapabilityUnavailableError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from dossier.core.secrets import get_secret
from dossier.llm.types import ChatResponse, JsonSchema, Message, Provider, RerankResult

log = logging.getLogger("dossier.llm")

_EMBED_BATCH = 32


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _cannot_constrain(exc: ProviderResponseError) -> bool:
    """The server understood the request but cannot enforce a JSON schema."""
    text = exc.message.lower()
    return exc.status == 501 or (
        "structured output" in text and ("unavailable" in text or "not supported" in text)
    )


class _HttpProvider:
    def __init__(self, name: str, base_url: str, *, timeout_s: float, api_key: str | None) -> None:
        self.name = name
        # Models this server cannot constrain; asked once, then prompted only.
        self.unconstrained_models: set[str] = set()
        self.base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=10.0), headers=headers, trust_env=False
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(method, url, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderUnavailableError(f"{self.name}: cannot connect to {url}") from exc
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(f"{self.name}: request to {url} timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"{self.name}: {type(exc).__name__} at {url}") from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            raise ProviderResponseError(
                f"{self.name}: HTTP {response.status_code} from {path}: {detail}",
                status=response.status_code,
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise ProviderResponseError(f"{self.name}: non-JSON response from {path}") from exc

    def _post_with_schema_fallback(
        self, path: str, payload: dict[str, Any], schema_key: str, model: str
    ) -> tuple[Any, bool | None]:
        """POST; if the server cannot enforce the schema, retry once without it."""
        if schema_key not in payload:
            return self._request("POST", path, payload), None
        if model in self.unconstrained_models:
            payload.pop(schema_key)
            return self._request("POST", path, payload), False
        try:
            return self._request("POST", path, payload), True
        except ProviderResponseError as exc:
            if not _cannot_constrain(exc):
                raise
            log.info(
                "%s: server cannot constrain output for model %s; using prompted JSON",
                self.name,
                model,
            )
            self.unconstrained_models.add(model)
            payload.pop(schema_key)
            return self._request("POST", path, payload), False

    def close(self) -> None:
        self._client.close()


class OllamaProvider(_HttpProvider):
    """Ollama's native API (``/api/chat``, ``/api/embed``, ``/api/tags``)."""

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
        options: dict[str, Any] = {"temperature": temperature, "num_predict": max_tokens}
        if context_tokens:
            options["num_ctx"] = context_tokens
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    **({"images": [_b64(image) for image in m.images]} if m.images else {}),
                }
                for m in messages
            ],
            "stream": False,
            "options": options,
            "keep_alive": "10m",
        }
        if json_schema is not None:
            payload["format"] = json_schema
        if think is not None:
            payload["think"] = think
        if extra:
            payload.update(extra)
        start = time.monotonic()
        body, constrained = self._post_with_schema_fallback("/api/chat", payload, "format", model)
        try:
            text = str(body["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise ProviderResponseError(f"{self.name}: unexpected chat response shape") from exc
        return ChatResponse(
            text=text,
            model=model,
            prompt_tokens=body.get("prompt_eval_count"),
            completion_tokens=body.get("eval_count"),
            seconds=round(time.monotonic() - start, 3),
            constrained=constrained,
        )

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH):
            batch = texts[start : start + _EMBED_BATCH]
            body = self._request("POST", "/api/embed", {"model": model, "input": batch})
            try:
                vectors.extend([float(x) for x in row] for row in body["embeddings"])
            except (KeyError, TypeError) as exc:
                raise ProviderResponseError(f"{self.name}: unexpected embed response") from exc
        return vectors

    def rerank(self, model: str, query: str, documents: list[str]) -> list[RerankResult]:
        del model, query, documents
        raise CapabilityUnavailableError(
            f"{self.name}: Ollama has no rerank API; point the rerank role at a llama.cpp server"
        )

    def list_models(self) -> list[str]:
        body = self._request("GET", "/api/tags")
        return sorted(str(item.get("name", "")) for item in body.get("models", []))


class OpenAICompatibleProvider(_HttpProvider):
    """Any server implementing ``/v1/chat/completions`` (base_url ends in ``/v1``)."""

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
        # Context size is fixed by the server's launch settings, and each server
        # spells "disable thinking" differently, so both come through ``extra``.
        del context_tokens, think
        wire: list[dict[str, Any]] = []
        for m in messages:
            if m.images:
                parts: list[dict[str, Any]] = [{"type": "text", "text": m.content}]
                parts.extend(
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(i)}"}}
                    for i in m.images
                )
                wire.append({"role": m.role, "content": parts})
            else:
                wire.append({"role": m.role, "content": m.content})
        payload: dict[str, Any] = {
            "model": model,
            "messages": wire,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": json_schema, "strict": False},
            }
        if extra:
            payload.update(extra)
        start = time.monotonic()
        body, constrained = self._post_with_schema_fallback(
            "/chat/completions", payload, "response_format", model
        )
        try:
            text = str(body["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(f"{self.name}: unexpected chat response shape") from exc
        usage = body.get("usage") or {}
        return ChatResponse(
            text=text,
            model=model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            seconds=round(time.monotonic() - start, 3),
            constrained=constrained,
        )

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH):
            batch = texts[start : start + _EMBED_BATCH]
            body = self._request("POST", "/embeddings", {"model": model, "input": batch})
            try:
                rows = sorted(body["data"], key=lambda row: int(row["index"]))
                vectors.extend([float(x) for x in row["embedding"]] for row in rows)
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderResponseError(f"{self.name}: unexpected embed response") from exc
        return vectors

    def rerank(self, model: str, query: str, documents: list[str]) -> list[RerankResult]:
        body = self._request(
            "POST", "/rerank", {"model": model, "query": query, "documents": documents}
        )
        try:
            results = [
                RerankResult(index=int(r["index"]), score=float(r["relevance_score"]))
                for r in body["results"]
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderResponseError(f"{self.name}: unexpected rerank response") from exc
        return sorted(results, key=lambda r: r.score, reverse=True)

    def list_models(self) -> list[str]:
        body = self._request("GET", "/models")
        return sorted(str(item.get("id", "")) for item in body.get("data", []))


def build_provider(name: str, config: ProviderConfig) -> Provider:
    api_key = get_secret(config.api_key_secret) if config.api_key_secret else None
    if config.kind is ProviderKind.OLLAMA:
        return OllamaProvider(name, config.base_url, timeout_s=config.timeout_s, api_key=api_key)
    if config.kind is ProviderKind.OPENAI:
        return OpenAICompatibleProvider(
            name, config.base_url, timeout_s=config.timeout_s, api_key=api_key
        )
    from dossier.llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(name, api_key=api_key, timeout_s=config.timeout_s)
