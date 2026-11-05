from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from dossier import platform
from dossier.core.config import ProcessingStatus, ProviderConfig
from dossier.core.policy import set_processing
from dossier.core.workspace import Workspace, open_workspace
from dossier.eval.synthetic import build_corpus
from dossier.llm.fake import FakeProvider
from dossier.llm.types import JsonSchema, Message
from dossier.ops.init import init_workspace
from dossier.ops.sync import SyncOptions, run_sync
from dossier.platform import fake


@pytest.fixture(autouse=True)
def fake_platform(monkeypatch: pytest.MonkeyPatch) -> Iterator[fake.FakeState]:
    """Every test runs against the in-memory platform unless it opts out."""
    monkeypatch.delenv("DOSSIER_WORKSPACE", raising=False)
    state = fake.FakeState()
    platform.use(fake.build(state))
    yield state
    platform.use(None)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    init_workspace(tmp_path / "acme", name="Acme")
    return open_workspace(tmp_path / "acme")


def responder(messages: list[Message], schema: JsonSchema | None) -> str:
    properties = set((schema or {}).get("properties", {}))
    text = messages[-1].content
    if {"decisions", "actions", "questions"} <= properties:
        # Meeting window: one valid item of each kind, one with an invented time.
        return json.dumps(
            {
                "summary": "The team reviewed the rollout and assigned the checklist.",
                "decisions": [
                    {"text": "Keep the Lakeside region", "at": "00:05"},
                    {"text": "Invented decision", "at": "59:59"},
                ],
                "actions": [
                    {
                        "task": "Prepare the migration checklist",
                        "owner": "Sam",
                        "due": "Friday",
                        "at": "00:09",
                    },
                    {"task": "Book the venue", "owner": "Jordan", "due": "Monday", "at": "00:09"},
                ],
                "questions": [{"text": "Who approves the budget?", "at": "00:13"}],
                "topics": ["Rollout"],
            }
        )
    if "overview" in properties:
        return json.dumps({"overview": "A short rollout review.", "topics": ["rollout"]})
    if "one_line" in properties:
        return json.dumps({"one_line": "Folder with project files.", "summary": "Holds files."})
    if "topics" in properties and "summary" in properties:
        if "budget of 1.2 million" in text:
            return json.dumps(
                {
                    "summary": "Charter for the Falcon platform. A budget of 1.2 million was "
                    "approved.",
                    "key_points": ["Budget: 1.2 million"],
                    "topics": ["Budget", "falcon"],
                }
            )
        return json.dumps({"summary": "A project document.", "key_points": [], "topics": ["misc"]})
    if "summary" in properties:
        return json.dumps({"summary": "One part of a document.", "key_points": []})
    if "topics" in properties:
        return json.dumps({"topics": [{"key": "planning", "label": "Planning"}]})
    return "{}"


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(responder=responder)

    def factory(name: str, config: ProviderConfig) -> FakeProvider:
        del name, config
        return provider

    monkeypatch.setattr("dossier.llm.router.build_provider", factory)
    return provider


@pytest.fixture
def indexed(workspace: Workspace, fake_model: FakeProvider) -> Workspace:
    set_processing(workspace.paths.config, ProcessingStatus.APPROVED)
    build_corpus(workspace.paths.sources)
    ws = open_workspace(workspace.root)
    outcome = run_sync(ws, SyncOptions())
    assert outcome.exit_status == 0, outcome.stages
    return ws
