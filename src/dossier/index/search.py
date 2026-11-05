"""Hybrid search over the workspace: keyword (FTS5/BM25) + vectors, fused with RRF.

Reciprocal-rank fusion needs no score calibration between the two retrievers,
which matters with small embedding models. An optional ``rerank`` role reorders
the fused top candidates. Results are chunk-level with provenance (document,
heading, line range) and at most two hits per document.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from dossier.core.errors import DossierError, ProviderUnavailableError
from dossier.core.workspace import Workspace
from dossier.index.store import ChunkRow, SearchStore
from dossier.llm.router import ModelRouter

RRF_K = 60
_TOKEN = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "and",
        "or",
        "to",
        "in",
        "on",
        "for",
        "with",
        "what",
        "which",
        "who",
        "when",
        "where",
        "how",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "did",
        "de",
        "la",
        "el",
        "los",
        "las",
        "y",
        "o",
        "en",
        "que",
        "qué",
        "cuál",
        "cuáles",
        "para",
        "por",
        "con",
    ]
)


@dataclass(frozen=True)
class Hit:
    rank: int
    score: float
    doc: str
    title: str
    kind: str
    type: str
    heading: str
    lines: str
    snippet: str
    corpus: str
    card: str
    matched_by: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class SearchFilters:
    source: str | None = None
    path_prefix: str | None = None
    doc_type: str | None = None
    kind: str | None = None

    def accept(self, row: ChunkRow) -> bool:
        if self.source and not row.doc_key.startswith(self.source + "/"):
            return False
        if self.path_prefix and not row.doc_key.startswith(self.path_prefix):
            return False
        if self.doc_type and row.doc_type != self.doc_type:
            return False
        return not (self.kind and row.kind != self.kind)


def fts_query(text: str) -> str:
    """Quote every meaningful token and OR them; BM25 rewards chunks matching more."""
    tokens = [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]
    return " OR ".join(f'"{token}"' for token in dict.fromkeys(tokens))


def _snippet(text: str, query: str, width: int = 280) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    tokens = [t for t in _TOKEN.findall(query.lower()) if t not in _STOP and len(t) > 2]
    lower = flat.lower()
    positions = [lower.find(t) for t in tokens if lower.find(t) >= 0]
    start = max(0, min(positions) - 60) if positions else 0
    piece = flat[start : start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(flat) else "")


def search(
    workspace: Workspace,
    query: str,
    *,
    limit: int = 10,
    filters: SearchFilters | None = None,
    per_doc: int = 2,
) -> tuple[list[Hit], list[str]]:
    """Return hits and notes (e.g. 'keyword only: embedding model unreachable')."""
    filters = filters or SearchFilters()
    notes: list[str] = []
    pool = max(50, limit * 8)
    db_path = workspace.paths.state / "search.db"
    if not db_path.exists():
        raise DossierError("no search index yet; run `dsr sync` (or `dsr index`) first")
    router = ModelRouter(workspace.config)
    with SearchStore(db_path) as store:
        keyword = store.keyword(fts_query(query), pool)
        vector: list[tuple[int, float]] = []
        signature = store.vector_signature()
        if signature and router.has("embed"):
            try:
                embed = router.client("embed")
                if not signature.startswith(embed.model + "|"):
                    notes.append("vectors were built with another model; run `dsr index --force`")
                else:
                    query_vector = embed.embed([embed.route.query_prefix + query])[0]
                    vector = store.nearest(query_vector, pool)
            except ProviderUnavailableError:
                notes.append("keyword search only: embedding model unreachable")
        elif not signature:
            notes.append("keyword search only: no vectors in the index")
        fused: dict[int, float] = {}
        sources: dict[int, set[str]] = {}
        for name, ranked in (("keyword", keyword), ("vector", vector)):
            for position, (chunk_id, _) in enumerate(ranked, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + position)
                sources.setdefault(chunk_id, set()).add(name)
        ordered = sorted(fused, key=lambda cid: fused[cid], reverse=True)
        rows = store.chunks(ordered)
    candidates = [rows[cid] for cid in ordered if cid in rows and filters.accept(rows[cid])]
    if router.has("rerank") and candidates:
        try:
            rerank = router.client("rerank")
            top = candidates[: max(limit * 3, 20)]
            ranking = rerank.rerank(query, [f"{r.title}\n{r.heading}\n{r.text}" for r in top])
            candidates = [top[r.index] for r in ranking] + candidates[len(top) :]
            notes.append("reranked")
        except DossierError as exc:
            notes.append(f"rerank skipped: {exc.code}")
    hits: list[Hit] = []
    per: dict[str, int] = {}
    for row in candidates:
        if per.get(row.doc_key, 0) >= per_doc:
            continue
        per[row.doc_key] = per.get(row.doc_key, 0) + 1
        lines = f"{row.line_start}-{row.line_end}" if row.line_start else ""
        hits.append(
            Hit(
                rank=len(hits) + 1,
                score=round(fused[row.chunk_id], 5),
                doc=row.doc_key,
                title=row.title,
                kind=row.kind,
                type=row.doc_type,
                heading=row.heading,
                lines=lines,
                snippet=_snippet(row.text, query),
                corpus=row.md_path,
                card=row.card_path,
                matched_by="+".join(sorted(sources.get(row.chunk_id, set()))),
            )
        )
        if len(hits) >= limit:
            break
    return hits, notes
