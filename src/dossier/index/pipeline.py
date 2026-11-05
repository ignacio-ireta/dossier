"""The index stage: cards, chunks, embeddings, folder summaries, tree documents.

Inputs are the converted documents listed in the convert snapshots (plus any
other registered collectors, e.g. meetings). The stage compares each document's
output hash with the index manifest and only rebuilds what changed. If the
``index`` model is unreachable, cards fall back to deterministic summaries
(marked ``degraded``) and are regenerated on a later run; if the ``embed`` model
is unreachable, keyword search keeps working and vectors are added later.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from dossier.convert.discovery import matches
from dossier.convert.snapshot import load_snapshot
from dossier.core.errors import ProviderUnavailableError
from dossier.core.io import atomic_write_text
from dossier.core.paths import WorkspacePaths
from dossier.core.workspace import Workspace
from dossier.index.cards import CARD_VERSION, build_card, render_card
from dossier.index.chunking import CHUNKER_VERSION, Chunk, chunk_markdown, outline
from dossier.index.folders import (
    DocEntry,
    FolderState,
    build_tree,
    summarize_folders,
    write_folder_pages,
    write_overview,
)
from dossier.index.store import DocRecord, SearchStore
from dossier.llm.router import ModelRouter, RoleClient

log = logging.getLogger("dossier.index")

_EMBED_BATCH = 32


@dataclass(frozen=True)
class Indexable:
    doc_key: str
    md_path: str  # workspace-relative
    output_sha256: str
    title: str
    doc_type: str
    kind: str = "document"


Collector = Callable[[Workspace], list[Indexable]]


def corpus_documents(workspace: Workspace) -> list[Indexable]:
    items: list[Indexable] = []
    for source in workspace.config.sources:
        root = workspace.paths.resolve(source.path)
        snapshot = load_snapshot(workspace.paths, source.name, root)
        for relpath, state in snapshot.files.items():
            if state.status != "ok" or not state.output or not state.output_sha256:
                continue
            items.append(
                Indexable(
                    doc_key=f"{source.name}/{relpath}",
                    md_path=f"corpus/{state.output}",
                    output_sha256=state.output_sha256,
                    title=state.title or PurePosixPath(relpath).name,
                    doc_type=state.extractor.split("@", 1)[0],
                )
            )
    return items


COLLECTORS: list[Collector] = [corpus_documents]


def register_collector(collector: Collector) -> None:
    if collector not in COLLECTORS:
        COLLECTORS.append(collector)


class DocState(BaseModel):
    output_sha256: str
    signature: str
    card: str
    status: str
    title: str
    doc_type: str
    kind: str = "document"
    one_line: str = ""
    topics: list[str] = Field(default_factory=list)
    corpus: str = ""


class IndexManifest(BaseModel):
    schema_version: int = 1
    docs: dict[str, DocState] = Field(default_factory=dict)
    folders: dict[str, FolderState] = Field(default_factory=dict)


def load_manifest(paths: WorkspacePaths) -> IndexManifest:
    path = paths.state / "state" / "index" / "manifest.json"
    if path.is_file():
        try:
            return IndexManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("index manifest unreadable; rebuilding")
    return IndexManifest()


def save_manifest(paths: WorkspacePaths, manifest: IndexManifest) -> None:
    atomic_write_text(
        paths.state / "state" / "index" / "manifest.json",
        manifest.model_dump_json(indent=1) + "\n",
    )


def card_path(doc_key: str) -> str:
    return f"index/cards/{doc_key}.card.md"


def _available(client: RoleClient) -> bool:
    try:
        client.provider.list_models()
    except ProviderUnavailableError:
        return False
    return True


def _authority(workspace: Workspace, doc_key: str) -> str | None:
    for rule in workspace.config.authority:
        if matches(doc_key, [rule.glob]):
            return f"tier {rule.tier}" + (f": {rule.label}" if rule.label else "")
    return None


@dataclass
class IndexResult:
    counts: dict[str, int] = field(default_factory=dict)
    attention: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def _topic_candidates(
    workspace: Workspace, embed: RoleClient | None, text: str, cache: dict[str, list[float]]
) -> list[str] | None:
    topics = workspace.config.taxonomy.topics
    if not topics:
        return None
    keys = [topic.key for topic in topics]
    if embed is None or len(keys) <= 8:
        return keys[:30]
    if not cache:
        vectors = embed.embed(
            [f"{embed.route.document_prefix}{t.label or t.key}. {t.description}" for t in topics]
        )
        cache.update(dict(zip(keys, vectors, strict=True)))
    query = embed.embed([embed.route.query_prefix + text[:2000]])[0]

    def score(key: str) -> float:
        return sum(a * b for a, b in zip(query, cache[key], strict=False))

    return sorted(keys, key=score, reverse=True)[:8]


def run_index(
    workspace: Workspace,
    *,
    run_id: str,
    scheduled: bool = False,
    force: bool = False,
    dry_run: bool = False,
    progress: Callable[[str, str], None] | None = None,
) -> IndexResult:
    from dossier.meet.collect import meeting_documents

    register_collector(meeting_documents)
    paths = workspace.paths
    config = workspace.config
    result = IndexResult()
    items = {item.doc_key: item for collector in COLLECTORS for item in collector(workspace)}
    manifest = load_manifest(paths)
    router = ModelRouter(config, scheduled=scheduled)

    chat: RoleClient | None = router.client("index") if router.has("index") else None
    if chat is not None and not _available(chat):
        result.attention.append("index model unreachable; cards use fallbacks until it is back")
        chat = None
    embed: RoleClient | None = router.client("embed") if router.has("embed") else None
    if embed is not None and not _available(embed):
        result.attention.append("embedding model unreachable; vectors will be added later")
        embed = None

    signature = f"card{CARD_VERSION}|chunk{CHUNKER_VERSION}|{chat.model if chat else 'none'}"
    todo: list[Indexable] = []
    for key, item in items.items():
        state = manifest.docs.get(key)
        stale = (
            state is None
            or force
            or state.output_sha256 != item.output_sha256
            or (state.signature != signature and chat is not None)
            or (state.status == "degraded" and chat is not None and config.index.redo_degraded)
        )
        if stale:
            todo.append(item)
    removed = [key for key in manifest.docs if key not in items]
    result.counts = {
        "documents": len(items),
        "to_index": len(todo),
        "to_remove": len(removed),
    }
    if dry_run:
        return result

    topic_cache: dict[str, list[float]] = {}
    created = updated = degraded = calls = 0
    with SearchStore(paths.state / "search.db") as store:
        for key in removed:
            state = manifest.docs.pop(key)
            (paths.root / state.card).unlink(missing_ok=True)
            store.delete_doc(key)
            if progress:
                progress("removed", key)
        for item in todo:
            text = (paths.root / item.md_path).read_text(encoding="utf-8")
            chunks = chunk_markdown(text, max_tokens=config.index.chunk_tokens)
            entries = outline(text)
            candidates = _topic_candidates(workspace, embed, text, topic_cache)
            card = build_card(
                chat,
                config,
                title=item.title,
                markdown=text,
                outline=entries,
                topic_candidates=candidates,
                max_windows=config.index.max_windows_per_document,
            )
            calls += card.calls
            card_rel = card_path(item.doc_key)
            atomic_write_text(
                paths.root / card_rel,
                render_card(
                    card,
                    doc_key=item.doc_key,
                    md_path=item.md_path,
                    doc_type=item.doc_type,
                    words=len(text.split()),
                    authority=_authority(workspace, item.doc_key),
                ),
            )
            summary_chunk = Chunk(
                -1, "Card summary", "\n".join([card.summary, *card.key_points]), 0, 0
            )
            store.replace_doc(
                DocRecord(
                    item.doc_key,
                    item.kind,
                    item.title,
                    item.doc_type,
                    item.md_path,
                    card_rel,
                    item.output_sha256,
                ),
                [summary_chunk, *chunks],
            )
            if item.doc_key in manifest.docs:
                updated += 1
            else:
                created += 1
            degraded += card.status == "degraded"
            manifest.docs[item.doc_key] = DocState(
                output_sha256=item.output_sha256,
                signature=signature,
                card=card_rel,
                status=card.status,
                title=item.title,
                doc_type=item.doc_type,
                kind=item.kind,
                one_line=card.one_line,
                topics=card.topics,
                corpus=item.md_path,
            )
            result.changed.append(item.doc_key)
            if progress:
                progress(card.status, item.doc_key)
        save_manifest(paths, manifest)

        embedded = 0
        if embed is not None and store.vec_available:
            probe = embed.embed([embed.route.document_prefix + "dimension probe"])[0]
            signature_vec = f"{embed.model}|{embed.route.document_prefix}"
            if store.prepare_vectors(signature_vec, len(probe)):
                while True:
                    batch = store.pending_embeddings(_EMBED_BATCH)
                    if not batch:
                        break
                    vectors = embed.embed([embed.route.document_prefix + t for _, t in batch])
                    embedded += store.store_embeddings(
                        (chunk_id, vector)
                        for (chunk_id, _), vector in zip(batch, vectors, strict=True)
                    )
        elif embed is not None and not store.vec_available:
            result.attention.append("vector search needs the embed extra (sqlite-vec)")
        stats = store.stats()

    docs = [
        DocEntry(key, s.title, s.doc_type, s.one_line, s.topics, s.card, s.corpus, s.status, s.kind)
        for key, s in sorted(manifest.docs.items())
    ]
    folders = build_tree(docs)
    states, folders_updated, folder_calls = summarize_folders(
        chat, config, folders, manifest.folders
    )
    manifest.folders = states
    save_manifest(paths, manifest)
    write_folder_pages(paths, folders, states)
    write_overview(paths, config, docs, folders, states, changed=result.changed, run_id=run_id)
    from dossier.agent.navigation import write_navigation

    write_navigation(workspace)
    result.counts.update(
        {
            "cards_created": created,
            "cards_updated": updated,
            "cards_removed": len(removed),
            "cards_degraded": degraded,
            "folders_updated": folders_updated,
            "model_calls": calls + folder_calls,
            "chunks": stats["chunks"],
            "chunks_embedded": stats["embedded"],
            "embedded_now": embedded,
        }
    )
    return result
