"""Resolve a document reference and return its converted text, card, or a line range.

A reference can be a document key (``main/plans/charter.docx``), a corpus path
(``corpus/main/plans/charter.docx.md``), a card path, or a meeting id.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dossier.core.errors import DossierError
from dossier.core.workspace import Workspace
from dossier.index.pipeline import load_manifest


@dataclass(frozen=True)
class Resolved:
    doc_key: str
    corpus: Path
    card: Path | None


def resolve(workspace: Workspace, reference: str) -> Resolved:
    ref = reference.strip().strip("/")
    manifest = load_manifest(workspace.paths)
    if ref in manifest.docs:
        state = manifest.docs[ref]
        return Resolved(ref, workspace.root / state.corpus, workspace.root / state.card)
    for key, state in manifest.docs.items():
        if ref in {state.corpus, state.card, state.corpus.removeprefix("corpus/")}:
            return Resolved(key, workspace.root / state.corpus, workspace.root / state.card)
    for candidate in (f"corpus/{ref}.md", f"corpus/{ref}", ref):
        path = workspace.root / candidate
        if path.is_file() and path.resolve().is_relative_to(workspace.root.resolve()):
            return Resolved(ref, path, None)
    raise DossierError(f"no document matches {reference!r}; try `dsr search` or `dsr tree`")


def parse_lines(spec: str | None) -> tuple[int, int] | None:
    if not spec:
        return None
    start, _, end = spec.partition("-")
    try:
        first = max(1, int(start))
        last = int(end) if end else first
    except ValueError as exc:
        raise DossierError(f"invalid line range {spec!r}; use 120-180") from exc
    if last < first:
        raise DossierError(f"invalid line range {spec!r}")
    return first, last


def read_document(
    workspace: Workspace,
    reference: str,
    *,
    lines: str | None = None,
    card: bool = False,
    max_chars: int | None = None,
) -> dict[str, object]:
    resolved = resolve(workspace, reference)
    target = resolved.card if card else resolved.corpus
    if target is None or not target.is_file():
        raise DossierError(f"{'card' if card else 'converted text'} not found for {reference!r}")
    text = target.read_text(encoding="utf-8")
    all_lines = text.splitlines()
    span = parse_lines(lines)
    if span:
        first, last = span
        text = "\n".join(all_lines[first - 1 : last])
    else:
        first, last = 1, len(all_lines)
    truncated = False
    if max_chars is not None and len(text) > max_chars:
        text, truncated = text[:max_chars], True
    return {
        "doc": resolved.doc_key,
        "path": str(target.relative_to(workspace.root)),
        "lines": f"{first}-{min(last, len(all_lines))}",
        "total_lines": len(all_lines),
        "truncated": truncated,
        "text": text,
    }
