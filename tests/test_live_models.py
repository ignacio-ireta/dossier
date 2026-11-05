"""Live checks against the local model server (``-m live_model``).

Skipped unless an Ollama server on 127.0.0.1:11434 offers the workspace's
default models. Sends only a fixed probe sentence, never workspace content.
"""

from __future__ import annotations

import httpx
import pytest

from dossier.core.workspace import Workspace
from dossier.llm.router import ModelRouter
from dossier.ops.models import probe_role

pytestmark = pytest.mark.live_model


def _offered() -> set[str]:
    try:
        response = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2, trust_env=False)
        return {model["name"] for model in response.json().get("models", [])}
    except (httpx.HTTPError, ValueError):
        return set()


@pytest.fixture
def router(workspace: Workspace) -> ModelRouter:
    offered = _offered()
    needed = {workspace.config.models["index"].model, "embeddinggemma:latest"}
    if not needed <= offered:
        pytest.skip(f"local server does not offer {sorted(needed - offered)}")
    return ModelRouter(workspace.config)


def test_small_chat_model_returns_valid_json(router: ModelRouter) -> None:
    result = probe_role(router, "index")
    assert result.status in {"ok", "repaired"}, result


def test_embedding_model(router: ModelRouter) -> None:
    result = probe_role(router, "embed")
    assert result.status == "ok", result
    assert "dimensions" in result.detail
