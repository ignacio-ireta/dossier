"""Anthropic adapter: request shape and error mapping, against a mock transport."""

from __future__ import annotations

import json
from typing import Any

import httpx2 as httpx
import pytest

anthropic = pytest.importorskip("anthropic")

from dossier.core.errors import (  # noqa: E402
    ConfigError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from dossier.llm.anthropic_provider import (  # noqa: E402
    AnthropicProvider,
    to_structured_output_schema,
)
from dossier.llm.structured import schema_for  # noqa: E402
from dossier.llm.types import Message  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def _provider(handler: Any) -> tuple[AnthropicProvider, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        result: httpx.Response = handler(request)
        return result

    provider = AnthropicProvider("cloud", api_key="test-key", timeout_s=5)
    provider._client = anthropic.Anthropic(
        api_key="test-key",
        base_url="http://anthropic.test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(record)),
    )
    return provider, seen


def _message(text: str, stop_reason: str = "end_turn") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 4},
        },
    )


def test_request_shape() -> None:
    provider, seen = _provider(lambda request: _message('{"ok": true}'))
    response = provider.chat(
        "claude-opus-5-5",
        [Message("system", "Be brief."), Message("user", "Look", images=(PNG,))],
        json_schema={
            "type": "object",
            "properties": {"title": {"type": "string", "maxLength": 40}},
            "required": ["title"],
        },
        max_tokens=16000,
        temperature=0.0,
        think=False,
        extra={"output_config": {"effort": "low"}},
    )
    assert response.text == '{"ok": true}'
    assert response.prompt_tokens == 11
    body = seen[0]
    assert "temperature" not in body and "thinking" not in body
    assert body["system"] == "Be brief."
    assert body["output_config"]["effort"] == "low"
    schema = body["output_config"]["format"]["schema"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert "maxLength" not in json.dumps(schema)
    image, text = body["messages"][0]["content"]
    assert image["source"]["media_type"] == "image/png"
    assert text == {"type": "text", "text": "Look"}


@pytest.mark.parametrize(
    ("stop_reason", "match"), [("refusal", "declined"), ("max_tokens", "max_tokens")]
)
def test_unusable_stop_reasons_raise(stop_reason: str, match: str) -> None:
    provider, _ = _provider(lambda request: _message("partial", stop_reason))
    with pytest.raises(ProviderResponseError, match=match):
        provider.chat("claude-opus-5-5", [Message("user", "u")])


def test_http_errors_map_to_dossier_errors() -> None:
    provider, _ = _provider(
        lambda request: httpx.Response(
            400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}}
        )
    )
    with pytest.raises(ProviderResponseError, match="HTTP 400"):
        provider.chat("claude-opus-5-5", [Message("user", "u")])

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider, _ = _provider(unreachable)
    with pytest.raises(ProviderUnavailableError):
        provider.chat("claude-opus-5-5", [Message("user", "u")])


def test_missing_key_is_config_error() -> None:
    with pytest.raises(ConfigError, match="API key"):
        AnthropicProvider("cloud", api_key=None, timeout_s=5)


def test_schema_transform_closes_nested_objects() -> None:
    from pydantic import BaseModel, Field

    class Inner(BaseModel):
        label: str = Field(min_length=1)

    class Outer(BaseModel):
        items: list[Inner] = Field(max_length=3)

    schema = to_structured_output_schema(schema_for(Outer))
    assert schema["properties"]["items"]["items"]["additionalProperties"] is False
    text = json.dumps(schema)
    assert "minLength" not in text and "maxItems" not in text
