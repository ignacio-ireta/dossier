"""Wire-format tests against a tiny in-process HTTP stub of Ollama and OpenAI servers."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest

from dossier.core.errors import (
    CapabilityUnavailableError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from dossier.llm.providers import OllamaProvider, OpenAICompatibleProvider
from dossier.llm.types import Message


class _Stub(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": "gemma4:e2b"}, {"name": "embeddinggemma:latest"}]})
        elif self.path == "/v1/models":
            self._send(200, {"data": [{"id": "small-model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        _Stub.requests.append((self.path, payload))
        if self.path == "/api/chat":
            if payload["model"] == "nogrammar" and "format" in payload:
                self._send(501, {"error": "structured output is unavailable"})
                return
            if payload["model"] == "missing":
                self._send(404, {"error": "model 'missing' not found"})
                return
            self._send(
                200,
                {"message": {"content": '{"ok": true}'}, "prompt_eval_count": 12, "eval_count": 5},
            )
        elif self.path == "/api/embed":
            self._send(200, {"embeddings": [[0.1, 0.2] for _ in payload["input"]]})
        elif self.path == "/v1/chat/completions":
            self._send(
                200,
                {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 3},
                },
            )
        elif self.path == "/v1/embeddings":
            data = [{"index": i, "embedding": [float(i)]} for i in range(len(payload["input"]))]
            self._send(200, {"data": list(reversed(data))})
        elif self.path == "/v1/rerank":
            self._send(
                200,
                {
                    "results": [
                        {"index": 0, "relevance_score": 0.1},
                        {"index": 1, "relevance_score": 0.9},
                    ]
                },
            )
        else:
            self._send(500, {"error": "boom"})


@pytest.fixture
def stub() -> Iterator[str]:
    _Stub.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_ollama_chat_payload_and_parse(stub: str) -> None:
    provider = OllamaProvider("local", stub, timeout_s=5, api_key=None)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    response = provider.chat(
        "gemma4:e2b",
        [Message("system", "s"), Message("user", "u", images=(b"\x89PNG",))],
        json_schema=schema,
        max_tokens=64,
        context_tokens=8192,
        think=False,
    )
    assert response.text == '{"ok": true}'
    assert response.prompt_tokens == 12 and response.completion_tokens == 5
    path, payload = _Stub.requests[-1]
    assert path == "/api/chat"
    assert payload["format"] == schema
    assert payload["think"] is False
    assert payload["stream"] is False
    assert payload["options"] == {"temperature": 0.0, "num_predict": 64, "num_ctx": 8192}
    assert payload["messages"][1]["images"] == ["iVBORw=="]


def test_ollama_embed_batches_and_lists(stub: str) -> None:
    provider = OllamaProvider("local", stub, timeout_s=5, api_key=None)
    vectors = provider.embed("embeddinggemma", [f"t{i}" for i in range(70)])
    assert len(vectors) == 70
    assert [len(p["input"]) for path, p in _Stub.requests if path == "/api/embed"] == [32, 32, 6]
    assert provider.list_models() == ["embeddinggemma:latest", "gemma4:e2b"]
    with pytest.raises(CapabilityUnavailableError):
        provider.rerank("m", "q", ["d"])


def test_ollama_http_error_is_response_error(stub: str) -> None:
    provider = OllamaProvider("local", stub, timeout_s=5, api_key=None)
    with pytest.raises(ProviderResponseError, match="404"):
        provider.chat("missing", [Message("user", "u")])


def test_openai_compatible_wire_format(stub: str) -> None:
    provider = OpenAICompatibleProvider("llama", stub + "/v1", timeout_s=5, api_key="k")
    response = provider.chat(
        "small-model",
        [Message("user", "describe", images=(b"img",))],
        json_schema={"type": "object"},
        extra={"chat_template_kwargs": {"enable_thinking": False}},
    )
    assert response.text == '{"ok": true}'
    path, payload = _Stub.requests[-1]
    assert path == "/v1/chat/completions"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    content = payload["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert provider.embed("e", ["a", "b"]) == [[0.0], [1.0]]  # sorted by index
    assert [r.index for r in provider.rerank("r", "q", ["a", "b"])] == [1, 0]
    assert provider.list_models() == ["small-model"]


def test_unreachable_server_is_unavailable() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    provider = OllamaProvider("local", f"http://127.0.0.1:{port}", timeout_s=2, api_key=None)
    with pytest.raises(ProviderUnavailableError):
        provider.list_models()


def test_schema_fallback_when_server_cannot_constrain(stub: str) -> None:
    provider = OllamaProvider("local", stub, timeout_s=5, api_key=None)
    schema = {"type": "object"}
    first = provider.chat("nogrammar", [Message("user", "u")], json_schema=schema)
    assert first.constrained is False and first.text == '{"ok": true}'
    chats = [p for path, p in _Stub.requests if path == "/api/chat"]
    assert ["format" in p for p in chats] == [True, False]  # tried, then retried bare
    provider.chat("nogrammar", [Message("user", "u")], json_schema=schema)
    chats = [p for path, p in _Stub.requests if path == "/api/chat"]
    assert ["format" in p for p in chats] == [True, False, False]  # remembered
    assert provider.chat("gemma4:e2b", [Message("user", "u")], json_schema=schema).constrained
    assert provider.chat("gemma4:e2b", [Message("user", "u")]).constrained is None
