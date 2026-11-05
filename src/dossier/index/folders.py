"""Folder summaries (bottom-up) and the workspace-level TREE.md / INDEX.md / index.json.

A folder's summary is built from the one-line summaries of what it contains
(documents and subfolders), never from full documents, so the input stays small
enough for a 2B-class model at any depth. A folder is re-summarized only when the
hash of those one-liners changes.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from dossier.core.config import WorkspaceConfig
from dossier.core.io import atomic_write_json, atomic_write_text, sha256_text
from dossier.core.paths import WorkspacePaths
from dossier.llm.budget import truncate_to_tokens
from dossier.llm.router import RoleClient
from dossier.llm.structured import Outcome, generate_structured

FOLDER_VERSION = 1
MAX_LISTED_DOCS = 15


class _Folder(BaseModel):
    one_line: str = Field(max_length=200, description="What this folder holds, in one line.")
    summary: str = Field(max_length=800, description="2-3 sentences.")


@dataclass
class DocEntry:
    doc_key: str
    title: str
    doc_type: str
    one_line: str
    topics: list[str]
    card: str  # workspace-relative
    corpus: str
    status: str
    kind: str = "document"


@dataclass
class FolderEntry:
    key: str  # e.g. "main" or "main/plans"
    docs: list[DocEntry] = field(default_factory=list)
    subfolders: list[str] = field(default_factory=list)


class FolderState(BaseModel):
    children_hash: str
    one_line: str
    summary: str
    status: str
    version: int = FOLDER_VERSION


def folder_of(doc_key: str) -> str:
    parent = str(PurePosixPath(doc_key).parent)
    return parent


def build_tree(docs: list[DocEntry]) -> dict[str, FolderEntry]:
    folders: dict[str, FolderEntry] = {}

    def ensure(key: str) -> FolderEntry:
        if key not in folders:
            folders[key] = FolderEntry(key)
            parent = str(PurePosixPath(key).parent)
            if parent not in {".", ""}:
                ensure(parent).subfolders.append(key)
        return folders[key]

    for doc in sorted(docs, key=lambda d: d.doc_key):
        ensure(folder_of(doc.doc_key)).docs.append(doc)
    for entry in folders.values():
        entry.subfolders = sorted(set(entry.subfolders))
    return folders


def _children_lines(entry: FolderEntry, states: dict[str, FolderState]) -> list[str]:
    lines = [f"document: {d.title} ({d.doc_type}) - {d.one_line}" for d in entry.docs]
    for sub in entry.subfolders:
        state = states.get(sub)
        name = PurePosixPath(sub).name
        lines.append(f"folder: {name}/ - {state.one_line if state else ''}")
    return lines


def _fallback(entry: FolderEntry, states: dict[str, FolderState]) -> _Folder:
    types = Counter(doc.doc_type for doc in entry.docs)
    topics = Counter(topic for doc in entry.docs for topic in doc.topics)
    parts = []
    if entry.docs:
        parts.append(
            f"{len(entry.docs)} documents ("
            + ", ".join(f"{n} {t}" for t, n in types.most_common(4))
            + ")"
        )
    if entry.subfolders:
        parts.append(f"{len(entry.subfolders)} subfolders")
    one_line = "; ".join(parts) or "empty"
    if topics:
        one_line += "; topics: " + ", ".join(t for t, _ in topics.most_common(4))
    titles = [doc.title for doc in entry.docs[:5]]
    subs = [PurePosixPath(sub).name for sub in entry.subfolders[:5]]
    summary = " ".join(
        part
        for part in (
            ("Documents include: " + "; ".join(titles) + ".") if titles else "",
            ("Subfolders: " + ", ".join(subs) + ".") if subs else "",
        )
        if part
    )
    del states
    return _Folder(one_line=one_line[:200], summary=summary[:800] or one_line)


def summarize_folders(
    client: RoleClient | None,
    config: WorkspaceConfig,
    folders: dict[str, FolderEntry],
    previous: dict[str, FolderState],
) -> tuple[dict[str, FolderState], int, int]:
    """Deepest folders first. Returns states, folders updated, model calls."""
    states: dict[str, FolderState] = {}
    updated = calls = 0
    for key in sorted(folders, key=lambda k: (-k.count("/"), k)):
        entry = folders[key]
        lines = _children_lines(entry, states)
        children_hash = sha256_text("\n".join(lines) + f"|v{FOLDER_VERSION}")
        old = previous.get(key)
        redo = old is not None and old.status == "degraded" and client is not None
        if old and old.children_hash == children_hash and not redo:
            states[key] = old
            continue
        if client is None:
            folder = _fallback(entry, states)
            status = "degraded"
        else:
            listing = truncate_to_tokens(
                "\n".join(lines), max(400, client.input_budget_tokens - 600)
            )
            brief = config.workspace.brief.strip()
            result = generate_structured(
                client,
                system=(
                    f'You describe folders in the workspace "{config.workspace.name}". '
                    + (f"Workspace context: {brief} " if brief else "")
                    + "Use only the listing. Do not invent content."
                ),
                user=(
                    f"Folder: {key}/\nContents:\n{listing}\n\nReturn: one_line (what this folder "
                    "holds, max 20 words), summary (2-3 sentences: main subjects and which "
                    "documents matter most)."
                ),
                schema=_Folder,
                fallback=lambda entry=entry: _fallback(entry, states),  # type: ignore[misc]
            )
            calls += result.attempts
            folder = result.value
            status = "degraded" if result.outcome is Outcome.DEGRADED else "ok"
        states[key] = FolderState(
            children_hash=children_hash,
            one_line=folder.one_line.strip(),
            summary=folder.summary.strip(),
            status=status,
        )
        updated += 1
    return states, updated, calls


def _link(from_dir: str, target: str) -> str:
    """Relative link from an index file's folder to a workspace-relative target."""
    depth = len(PurePosixPath(from_dir).parts) if from_dir else 0
    return "../" * depth + target


def write_folder_pages(
    paths: WorkspacePaths,
    folders: dict[str, FolderEntry],
    states: dict[str, FolderState],
) -> None:
    base = paths.index / "folders"
    for key, entry in folders.items():
        state = states[key]
        page_dir = f"index/folders/{key}"
        lines = [
            "---",
            f"folder: {json.dumps(key)}",
            f"documents: {len(entry.docs)}",
            f"subfolders: {len(entry.subfolders)}",
            f"status: {json.dumps(state.status)}",
            "---",
            "",
            f"# {key}/",
            "",
            state.summary or state.one_line,
            "",
        ]
        if entry.subfolders:
            lines += ["## Subfolders", ""]
            for sub in entry.subfolders:
                sub_state = states.get(sub)
                name = PurePosixPath(sub).name
                link = _link(page_dir, f"index/folders/{sub}/_folder.md")
                lines.append(f"- [{name}/]({link}) - {sub_state.one_line if sub_state else ''}")
            lines.append("")
        if entry.docs:
            lines += ["## Documents", "", "| Document | Type | Summary |", "| --- | --- | --- |"]
            for doc in entry.docs:
                link = _link(page_dir, doc.card)
                summary = doc.one_line.replace("|", "\\|")
                lines.append(f"| [{doc.title}]({link}) | {doc.doc_type} | {summary} |")
            lines.append("")
        atomic_write_text(base / key / "_folder.md", "\n".join(lines))


def write_overview(
    paths: WorkspacePaths,
    config: WorkspaceConfig,
    docs: list[DocEntry],
    folders: dict[str, FolderEntry],
    states: dict[str, FolderState],
    *,
    changed: list[str],
    run_id: str,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    roots = sorted(key for key in folders if "/" not in key)
    degraded = sum(1 for doc in docs if doc.status == "degraded")

    tree = [
        f"# Tree: {config.workspace.name}",
        "",
        f"{len(docs)} documents in {len(folders)} folders. Updated {now}.",
        "",
    ]

    def walk(key: str, depth: int) -> None:
        entry = folders[key]
        state = states.get(key)
        name = key if depth == 0 else PurePosixPath(key).name
        link = f"folders/{key}/_folder.md"
        tree.append(f"{'  ' * depth}- **[{name}/]({link})** - {state.one_line if state else ''}")
        for sub in entry.subfolders:
            walk(sub, depth + 1)
        listed = entry.docs[:MAX_LISTED_DOCS]
        for doc in listed:
            card = doc.card.removeprefix("index/")
            tree.append(f"{'  ' * (depth + 1)}- [{doc.title}]({card}) - {doc.one_line}")
        if len(entry.docs) > len(listed):
            tree.append(
                f"{'  ' * (depth + 1)}- ... {len(entry.docs) - len(listed)} more "
                "(see the folder summary)"
            )

    for root in roots:
        walk(root, 0)
    atomic_write_text(paths.index / "TREE.md", "\n".join(tree) + "\n")

    by_topic: dict[str, list[DocEntry]] = defaultdict(list)
    for doc in docs:
        for topic in doc.topics:
            by_topic[topic].append(doc)
    by_type = Counter(doc.doc_type for doc in docs)
    index = [f"# Index: {config.workspace.name}", ""]
    if config.workspace.brief.strip():
        index += [config.workspace.brief.strip(), ""]
    index += [
        "## How to navigate",
        "",
        "1. `TREE.md`: the whole workspace, one line per folder and document.",
        "2. `folders/<path>/_folder.md`: what a folder holds and which documents matter.",
        "3. `cards/<path>.card.md`: a document's summary, key points and outline with line "
        "numbers.",
        "4. The converted document itself: `dsr read <doc> --lines a-b`, or search with "
        '`dsr search "<question>"`.',
        "",
        "## Freshness",
        "",
        f"- Index updated: {now} (run `{run_id}`)",
        f"- Documents: {len(docs)}; cards generated without a model: {degraded}",
        f"- Changed in the last run: {len(changed)}",
        "",
        "## Sources",
        "",
    ]
    for root in roots:
        state = states.get(root)
        index.append(f"- [{root}/](folders/{root}/_folder.md) - {state.one_line if state else ''}")
    index += ["", "## By type", ""]
    index += [f"- {doc_type}: {count}" for doc_type, count in by_type.most_common()]
    if by_topic:
        index += ["", "## By topic", ""]
        for topic in sorted(by_topic, key=lambda t: (-len(by_topic[t]), t))[:40]:
            index.append(f"### {topic}")
            index.append("")
            for doc in by_topic[topic][:10]:
                index.append(f"- [{doc.title}]({doc.card.removeprefix('index/')}) - {doc.one_line}")
            index.append("")
    if changed:
        index += ["## Changed in the last run", ""]
        lookup = {doc.doc_key: doc for doc in docs}
        for key in changed[:30]:
            changed_doc = lookup.get(key)
            if changed_doc:
                card_link = changed_doc.card.removeprefix("index/")
                index.append(f"- [{changed_doc.title}]({card_link})")
        index.append("")
    atomic_write_text(paths.index / "INDEX.md", "\n".join(index).rstrip() + "\n")
    atomic_write_json(
        paths.index / "index.json",
        {
            "workspace": config.workspace.name,
            "updated_at": now,
            "run_id": run_id,
            "documents": [
                {
                    "doc": d.doc_key,
                    "title": d.title,
                    "type": d.doc_type,
                    "kind": d.kind,
                    "folder": folder_of(d.doc_key),
                    "card": d.card,
                    "corpus": d.corpus,
                    "topics": d.topics,
                    "status": d.status,
                    "one_line": d.one_line,
                }
                for d in docs
            ],
            "folders": [
                {
                    "folder": key,
                    "one_line": states[key].one_line,
                    "summary": states[key].summary,
                    "documents": len(folders[key].docs),
                    "subfolders": folders[key].subfolders,
                    "status": states[key].status,
                }
                for key in sorted(folders)
            ],
        },
    )
