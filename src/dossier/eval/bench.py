"""``dsr models bench``: measure the configured models on the synthetic corpus.

Builds a throwaway workspace with the same providers and model roles as the
current one, converts and indexes the synthetic files, then reports:

- cards produced by the model vs. fallback cards,
- fact recall: the planted answer appears in its document's card,
- retrieval hit@5: searching the planted question returns the right document,
- time and number of model calls.

No workspace content is used; the corpus is invented.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomlkit

from dossier.core.config import ProcessingStatus
from dossier.core.io import atomic_write_text
from dossier.core.policy import set_processing
from dossier.core.workspace import Workspace, open_workspace
from dossier.eval.synthetic import build_corpus
from dossier.index.pipeline import load_manifest
from dossier.index.search import search
from dossier.ops.init import init_workspace
from dossier.ops.sync import SyncOptions, run_sync


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"(?<=\d)[,\s](?=\d{3}\b)", "", text)
    return re.sub(r"\s+", " ", text)


@dataclass
class BenchResult:
    index_model: str
    embed_model: str
    documents: int = 0
    cards_ok: int = 0
    cards_degraded: int = 0
    fact_recall: float = 0.0
    hit_at_5: float = 0.0
    index_seconds: float = 0.0
    model_calls: int = 0
    facts: list[dict[str, object]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _copy_models(source: Workspace, target_config: Path) -> None:
    original = tomlkit.parse(source.paths.config.read_text(encoding="utf-8"))
    document = tomlkit.parse(target_config.read_text(encoding="utf-8"))
    for table in ("providers", "models"):
        if table in original:
            document[table] = original[table]
    if "policy" in original:
        for key in ("allow_external", "allowed_providers"):
            if key in original["policy"]:
                document["policy"][key] = original["policy"][key]
    atomic_write_text(target_config, tomlkit.dumps(document))


def run_bench(source: Workspace | None, workdir: Path) -> BenchResult:
    paths = init_workspace(workdir, name="Benchmark (synthetic)", force=True)
    if source is not None:
        _copy_models(source, paths.config)
    facts = build_corpus(paths.sources)
    set_processing(paths.config, ProcessingStatus.APPROVED, note="synthetic benchmark corpus")
    workspace = open_workspace(workdir)
    models = workspace.config.models
    result = BenchResult(
        index_model=models["index"].model if "index" in models else "",
        embed_model=models["embed"].model if "embed" in models else "",
    )
    started = time.monotonic()
    outcome = run_sync(workspace, SyncOptions(stages=["convert", "index"]))
    result.index_seconds = round(time.monotonic() - started, 1)
    result.notes.extend(outcome.attention)
    index_counts = outcome.stages.get("index", {}).get("counts", {})
    result.model_calls = int(index_counts.get("model_calls", 0))
    manifest = load_manifest(workspace.paths)
    result.documents = len(manifest.docs)
    result.cards_ok = sum(1 for d in manifest.docs.values() if d.status == "ok")
    result.cards_degraded = result.documents - result.cards_ok
    recalled = hits = 0
    for fact in facts:
        doc_key = f"main/{fact.file}"
        state = manifest.docs.get(doc_key)
        card_text = (workspace.root / state.card).read_text(encoding="utf-8") if state else ""
        in_card = normalize(fact.answer) in normalize(card_text)
        found, notes = search(workspace, fact.question, limit=5)
        for note in notes:
            if note not in result.notes:
                result.notes.append(note)
        ranks = [hit.doc for hit in found]
        rank = ranks.index(doc_key) + 1 if doc_key in ranks else None
        recalled += in_card
        hits += rank is not None
        result.facts.append({"file": fact.file, "answer_in_card": in_card, "search_rank": rank})
    result.fact_recall = round(recalled / len(facts), 2)
    result.hit_at_5 = round(hits / len(facts), 2)
    return result
