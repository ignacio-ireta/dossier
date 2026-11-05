from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dossier.core.config import ProcessingStatus, ProviderConfig
from dossier.core.errors import DossierError, ProviderUnavailableError
from dossier.core.policy import set_processing
from dossier.core.workspace import Workspace, open_workspace
from dossier.eval.synthetic import build_corpus
from dossier.index.chunking import chunk_markdown, outline
from dossier.index.pipeline import load_manifest
from dossier.index.qmd import Qmd
from dossier.index.read import read_document
from dossier.index.search import SearchFilters, fts_query, search
from dossier.index.taxonomy import suggest
from dossier.llm.fake import FakeProvider
from dossier.llm.types import JsonSchema, Message
from dossier.ops.sync import SyncOptions, run_sync


def responder(messages: list[Message], schema: JsonSchema | None) -> str:
    properties = set((schema or {}).get("properties", {}))
    text = messages[-1].content
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


def test_cards_folders_and_overview(indexed: Workspace, fake_model: FakeProvider) -> None:
    manifest = load_manifest(indexed.paths)
    assert len(manifest.docs) == 9
    assert all(doc.status == "ok" for doc in manifest.docs.values())
    charter = indexed.paths.root / manifest.docs["main/plans/project-charter.docx"].card
    text = charter.read_text(encoding="utf-8")
    assert "1.2 million" in text and '"budget"' in text  # topics lowercased
    assert "## Where to find what" in text and "dsr read main/plans/project-charter.docx" in text
    assert (indexed.paths.index / "folders/main/plans/_folder.md").is_file()
    tree = (indexed.paths.index / "TREE.md").read_text(encoding="utf-8")
    assert "**[plans/](folders/main/plans/_folder.md)**" in tree
    overview = (indexed.paths.index / "INDEX.md").read_text(encoding="utf-8")
    assert "## How to navigate" in overview and "### budget" in overview
    data = json.loads((indexed.paths.index / "index.json").read_text(encoding="utf-8"))
    assert {d["doc"] for d in data["documents"]} == set(manifest.docs)
    for call in fake_model.calls:
        system = call["messages"][0].content
        assert "JSON Schema" in system and call["think"] is False


def test_incremental_index(indexed: Workspace, fake_model: FakeProvider) -> None:
    before = len(fake_model.calls)
    outcome = run_sync(indexed, SyncOptions())
    assert outcome.stages["index"]["counts"]["to_index"] == 0
    assert len(fake_model.calls) == before  # nothing changed: no model calls at all
    glossary = indexed.paths.sources / "notes" / "glossary.md"
    glossary.write_text(glossary.read_text() + "- **KPI**: key performance indicator.\n")
    (indexed.paths.sources / "notes" / "settings.json").unlink()
    outcome = run_sync(indexed, SyncOptions())
    counts = outcome.stages["index"]["counts"]
    assert counts["cards_updated"] == 1 and counts["cards_removed"] == 1
    assert counts["folders_updated"] >= 1  # notes/ and its parents changed
    manifest = load_manifest(indexed.paths)
    assert "main/notes/settings.json" not in manifest.docs
    assert not (indexed.paths.index / "cards/main/notes/settings.json.card.md").exists()


def test_unreachable_model_degrades_then_recovers(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    down = FakeProvider()

    def unavailable() -> list[str]:
        raise ProviderUnavailableError("down")

    down.list_models = unavailable  # type: ignore[method-assign]
    monkeypatch.setattr("dossier.llm.router.build_provider", lambda name, config: down)
    set_processing(workspace.paths.config, ProcessingStatus.APPROVED)
    build_corpus(workspace.paths.sources)
    ws = open_workspace(workspace.root)
    outcome = run_sync(ws, SyncOptions())
    assert outcome.exit_status == 75  # needs attention, not a failure
    manifest = load_manifest(ws.paths)
    assert all(doc.status == "degraded" for doc in manifest.docs.values())
    charter = (ws.paths.root / manifest.docs["main/plans/project-charter.docx"].card).read_text()
    assert "1.2 million" in charter  # the fallback uses the document's own sentences
    hits, notes = search(ws, "approved budget")
    assert hits and notes == ["keyword search only: no vectors in the index"]

    up = FakeProvider(responder=responder)
    monkeypatch.setattr("dossier.llm.router.build_provider", lambda name, config: up)
    outcome = run_sync(ws, SyncOptions())
    assert outcome.exit_status == 0
    assert outcome.stages["index"]["counts"]["cards_updated"] == 9
    assert all(doc.status == "ok" for doc in load_manifest(ws.paths).docs.values())


def test_hybrid_search_with_vectors(indexed: Workspace) -> None:
    hits, notes = search(indexed, "Which hosting option did the steering committee choose?")
    assert not notes
    assert hits[0].doc == "main/mail/steering-decision.eml"
    assert "keyword" in hits[0].matched_by and "vector" in hits[0].matched_by
    assert hits[0].corpus.startswith("corpus/main/mail/")
    assert len({h.doc for h in hits[:2]}) <= 2
    filtered, _ = search(indexed, "budget", filters=SearchFilters(doc_type="xlsx"))
    assert filtered and all(hit.type == "xlsx" for hit in filtered)


def test_fts_query_and_chunks() -> None:
    assert fts_query("What is the budget?") == '"budget"'
    assert fts_query("Reunión de presupuesto") == '"reunión" OR "presupuesto"'
    text = (
        '---\nsource: "x"\n---\n\n# Title\n\nIntro line.\n\n## Slide 1: A\n\nAlpha text.\n\n'
        "## Slide 2: B\n\n### Detail\n\nBeta text.\n\n## Extraction warnings\n\n- `X`: y\n"
    )
    chunks = chunk_markdown(text, max_tokens=200)
    assert [c.heading for c in chunks] == ["", "Slide 1: A", "Slide 2: B > Detail"]
    assert chunks[1].text == "Alpha text." and chunks[1].line_start == 11
    assert [e.text for e in outline(text)][:2] == ["Title", "Slide 1: A"]


def test_read_resolution(indexed: Workspace) -> None:
    whole = read_document(indexed, "main/plans/roadmap.pptx")
    assert "Slide 2: Phases" in str(whole["text"])
    part = read_document(indexed, "corpus/main/plans/roadmap.pptx.md", lines="1-3")
    assert part["lines"] == "1-3" and str(part["text"]).startswith("---")
    card = read_document(indexed, "main/plans/roadmap.pptx", card=True)
    assert str(card["path"]).startswith("index/cards/")
    with pytest.raises(DossierError):
        read_document(indexed, "../../etc/passwd")


def test_taxonomy_suggest(indexed: Workspace) -> None:
    topics, snippet = suggest(indexed)
    assert topics == [("planning", "Planning")]
    assert "[taxonomy]" in snippet
    assert (indexed.paths.index / "taxonomy-suggestion.toml").is_file()


def test_qmd_adapter_commands(indexed: Workspace) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        stdout = ""
        if command[3:5] == ["collection", "list"]:
            stdout = "no collections"
        if command[3] == "query":
            stdout = json.dumps(
                [
                    {
                        "file": f"qmd://{qmd.index_name}-main/mail/steering-decision.eml.md"
                        f"?index={qmd.index_name}",
                        "score": 0.91,
                        "title": "Steering decision",
                        "snippet": "@@ -21,4 @@ (20 before, 9 after)\n\nLakeside region chosen",
                    }
                ]
            )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    qmd = Qmd(indexed, runner=runner, executable="/usr/local/bin/qmd")
    counts = qmd.sync()
    verbs = [tuple(c[3:5]) for c in calls]
    assert ("collection", "add") in verbs and tuple(calls[-2][3:4]) == ("update",)
    add = next(c for c in calls if c[3:5] == ["collection", "add"])
    assert add[1:3] == ["--index", qmd.index_name] and add[-2:] == ["--mask", "**/*.md"]
    assert counts["contexts"] >= 1
    hits = qmd.search("hosting decision", limit=3)
    assert hits[0].doc == "main/mail/steering-decision.eml"
    assert hits[0].corpus == "corpus/main/mail/steering-decision.eml.md"
    assert hits[0].lines == "21-24" and hits[0].snippet == "Lakeside region chosen"


def test_qmd_missing_is_clear(indexed: Workspace) -> None:
    qmd = Qmd(indexed)
    qmd.executable = None  # as if not installed anywhere
    with pytest.raises(DossierError, match="bun install -g @tobilu/qmd"):
        qmd.search("x")


def test_bench_runs_on_synthetic_corpus(
    tmp_path: Path, fake_model: FakeProvider, workspace: Workspace
) -> None:
    from dossier.eval.bench import run_bench

    result = run_bench(workspace, tmp_path / "bench")
    assert result.documents == 9 and result.cards_degraded == 0
    assert result.facts[0]["answer_in_card"] is True  # the charter's budget
    assert 0.0 <= result.hit_at_5 <= 1.0
    data: dict[str, Any] = result.as_dict()
    assert data["index_model"] == "gemma4:e2b"
