from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from dossier.core.config import ProviderConfig, WorkspaceConfig
from dossier.core.errors import (
    ConfigError,
    PolicyBlockedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from dossier.core.workspace import Workspace
from dossier.llm.budget import estimate_tokens, pack, split_text, truncate_to_tokens
from dossier.llm.fake import FakeProvider, hash_embedding
from dossier.llm.router import ModelRouter
from dossier.llm.structured import Outcome, extract_json, generate_structured, schema_for
from dossier.llm.types import Provider


class Inner(BaseModel):
    label: str


class Card(BaseModel):
    title: str = Field(max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=3)
    inner: Inner | None = None


def _router(config: WorkspaceConfig, provider: FakeProvider) -> ModelRouter:
    def factory(name: str, cfg: ProviderConfig) -> Provider:
        del name, cfg
        return provider

    return ModelRouter(config, provider_factory=factory)


def test_schema_is_flat() -> None:
    schema = schema_for(Card)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text and '"title": "' not in text
    assert "title" in schema["properties"]  # a field named "title" survives
    assert schema["additionalProperties"] is False
    assert "label" in text


@pytest.mark.parametrize(
    "text",
    [
        '{"title": "x"}',
        '```json\n{"title": "x"}\n```',
        'Sure! Here it is: {"title": "x"} hope that helps',
        '{"broken": } then {"title": "x"}',
        '{"title": "x with } brace"}',
    ],
)
def test_extract_json_tolerates_chatter(text: str) -> None:
    assert extract_json(text)["title"].startswith("x")


def test_extract_json_raises_without_object() -> None:
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_structured_ok_on_first_try(workspace: Workspace) -> None:
    provider = FakeProvider(script=['{"title": "Budget review", "tags": ["finance"]}'])
    client = _router(workspace.config, provider).client("index")
    result = generate_structured(
        client, system="s", user="u", schema=Card, fallback=lambda: Card(title="fallback")
    )
    assert result.outcome is Outcome.OK
    assert result.value.title == "Budget review"
    call = provider.calls[0]
    assert call["json_schema"]["additionalProperties"] is False
    assert call["think"] is False  # small-model default from the workspace template


def test_structured_repairs_once(workspace: Workspace) -> None:
    provider = FakeProvider(script=['{"title": "' + "x" * 80 + '"}', '{"title": "short"}'])
    client = _router(workspace.config, provider).client("index")
    result = generate_structured(
        client, system="s", user="u", schema=Card, fallback=lambda: Card(title="fallback")
    )
    assert result.outcome is Outcome.REPAIRED
    assert result.attempts == 2
    repair_prompt = provider.calls[1]["messages"][-1].content
    assert "invalid" in repair_prompt and "title" in repair_prompt


def test_structured_degrades_to_fallback(workspace: Workspace) -> None:
    provider = FakeProvider(script=["not json", "still not json"])
    client = _router(workspace.config, provider).client("index")
    result = generate_structured(
        client, system="s", user="u", schema=Card, fallback=lambda: Card(title="fallback")
    )
    assert result.outcome is Outcome.DEGRADED
    assert result.value.title == "fallback"
    assert result.error


def test_provider_error_degrades_but_unavailable_raises(workspace: Workspace) -> None:
    provider = FakeProvider(script=[ProviderResponseError("HTTP 400: context too long")])
    client = _router(workspace.config, provider).client("index")
    result = generate_structured(
        client, system="s", user="u", schema=Card, fallback=lambda: Card(title="fallback")
    )
    assert result.outcome is Outcome.DEGRADED
    provider.script.append(ProviderUnavailableError("down"))
    with pytest.raises(ProviderUnavailableError):
        generate_structured(
            client, system="s", user="u", schema=Card, fallback=lambda: Card(title="fallback")
        )


def test_router_roles_and_policy(workspace: Workspace) -> None:
    provider = FakeProvider()
    router = _router(workspace.config, provider)
    assert router.has("index") and not router.has("vision") and not router.has("rerank")
    with pytest.raises(ConfigError, match="vision"):
        router.client("vision")
    assert router.client("embed").embed(["a b"]) == [hash_embedding("a b")]

    external = workspace.config.model_copy(deep=True)
    external.providers["local"] = ProviderConfig(
        kind="openai", base_url="https://models.example.com/v1"
    )
    with pytest.raises(PolicyBlockedError):
        _router(external, provider).client("index")


def test_budget_helpers() -> None:
    assert estimate_tokens("x" * 32) == 10
    long_text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(20))
    cut = truncate_to_tokens(long_text, 200)
    assert len(cut) <= 200 * 3.2 and cut.endswith("[…truncated]")
    pieces = split_text(long_text, 150)
    assert len(pieces) > 1
    assert all(estimate_tokens(p) <= 150 for p in pieces)
    assert "".join(pieces).replace("\n", "").count("Paragraph") == 20
    windows = pack(pieces, 300)
    assert [i for w in windows for i in w] == list(range(len(pieces)))
    huge = split_text("a" * 5000, 100)
    assert all(len(p) <= 320 for p in huge)


def test_prompt_shows_example_not_schema(workspace: Workspace) -> None:
    from dossier.llm.structured import example_for, unwrap

    provider = FakeProvider(script=['{"title": "x"}'])
    client = _router(workspace.config, provider).client("index")
    generate_structured(client, system="s", user="u", schema=Card, fallback=lambda: Card(title="f"))
    system = provider.calls[0]["messages"][0].content
    assert '{"title": "...", "tags": ["..."], "inner": {"label": "..."}}' in system
    assert "- title: at most 40 characters" in system
    assert example_for({"type": "object", "properties": {"n": {"type": "integer"}}}) == {"n": 0}
    schema = schema_for(Card)
    echoed = {"type": "object", "properties": {"title": "Budget", "tags": []}}
    assert unwrap(echoed, schema) == {"title": "Budget", "tags": []}
    bare = {"type": "object", "properties": {"title": {"type": "string"}}}
    assert unwrap(bare, schema) is bare


def test_echoed_schema_answer_is_recovered(workspace: Workspace) -> None:
    reply = '{"type": "object", "properties": {"title": "Recovered", "tags": ["a"]}}'
    provider = FakeProvider(script=[reply])
    client = _router(workspace.config, provider).client("index")
    result = generate_structured(
        client, system="s", user="u", schema=Card, fallback=lambda: Card(title="fallback")
    )
    assert result.outcome is Outcome.OK and result.value.title == "Recovered"
