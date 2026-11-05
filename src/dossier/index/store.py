"""The search database: chunks, an FTS5 keyword index, and a sqlite-vec index.

One SQLite file per workspace (``.dossier/search.db``). The vector table exists
only when the ``embed`` extra is installed and an embedding model is configured;
keyword search works without it. Changing the embedding model drops and rebuilds
the vectors.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from dossier.index.chunking import Chunk

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DocRecord:
    doc_key: str  # "<source>/<relpath>" or "meetings/<id>"
    kind: str  # document | meeting
    title: str
    doc_type: str
    md_path: str  # workspace-relative
    card_path: str  # workspace-relative
    output_sha256: str


@dataclass(frozen=True)
class ChunkRow:
    chunk_id: int
    doc_key: str
    ordinal: int
    heading: str
    text: str
    line_start: int
    line_end: int
    title: str
    kind: str
    doc_type: str
    md_path: str
    card_path: str


def _load_vec(connection: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError):
        return False
    return True


class SearchStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.vec_available = _load_vec(self.db)
        self._schema()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> SearchStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS docs (
                doc_key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                doc_type TEXT NOT NULL, md_path TEXT NOT NULL, card_path TEXT NOT NULL,
                output_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                doc_key TEXT NOT NULL REFERENCES docs(doc_key) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, heading TEXT NOT NULL, text TEXT NOT NULL,
                line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
                embedded INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_key);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                title, heading, text, chunk_id UNINDEXED,
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.db.commit()

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --- documents ---------------------------------------------------------

    def doc_keys(self) -> set[str]:
        return {str(row[0]) for row in self.db.execute("SELECT doc_key FROM docs")}

    def replace_doc(self, doc: DocRecord, chunks: Sequence[Chunk]) -> None:
        self.delete_doc(doc.doc_key, commit=False)
        self.db.execute(
            "INSERT INTO docs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                doc.doc_key,
                doc.kind,
                doc.title,
                doc.doc_type,
                doc.md_path,
                doc.card_path,
                doc.output_sha256,
            ),
        )
        for chunk in chunks:
            cursor = self.db.execute(
                "INSERT INTO chunks(doc_key, ordinal, heading, text, line_start, line_end) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    doc.doc_key,
                    chunk.ordinal,
                    chunk.heading,
                    chunk.text,
                    chunk.line_start,
                    chunk.line_end,
                ),
            )
            self.db.execute(
                "INSERT INTO chunks_fts(title, heading, text, chunk_id) VALUES (?, ?, ?, ?)",
                (doc.title, chunk.heading, chunk.text, cursor.lastrowid),
            )
        self.db.commit()

    def delete_doc(self, doc_key: str, *, commit: bool = True) -> None:
        ids = [
            row[0] for row in self.db.execute("SELECT id FROM chunks WHERE doc_key = ?", (doc_key,))
        ]
        if ids:
            marks = ",".join("?" * len(ids))
            self.db.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({marks})", ids)
            if self._has_vectors():
                self.db.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({marks})", ids)
        self.db.execute("DELETE FROM chunks WHERE doc_key = ?", (doc_key,))
        self.db.execute("DELETE FROM docs WHERE doc_key = ?", (doc_key,))
        if commit:
            self.db.commit()

    # --- vectors -----------------------------------------------------------

    def _has_vectors(self) -> bool:
        row = self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_chunks'").fetchone()
        return row is not None

    def prepare_vectors(self, model: str, dimensions: int) -> bool:
        """Create (or rebuild for a new model) the vector table. False if unavailable."""
        if not self.vec_available:
            return False
        signature = f"{model}|{dimensions}"
        if self.meta("embed_signature") != signature or not self._has_vectors():
            self.db.execute("DROP TABLE IF EXISTS vec_chunks")
            self.db.execute(
                f"CREATE VIRTUAL TABLE vec_chunks USING vec0("
                f"embedding float[{int(dimensions)}] distance_metric=cosine)"
            )
            self.db.execute("UPDATE chunks SET embedded = 0")
            self.set_meta("embed_signature", signature)
            self.db.commit()
        return True

    def vector_signature(self) -> str | None:
        return self.meta("embed_signature") if self._has_vectors() else None

    def pending_embeddings(self, limit: int) -> list[tuple[int, str]]:
        rows = self.db.execute(
            "SELECT c.id, d.title, c.heading, c.text FROM chunks c JOIN docs d USING(doc_key) "
            "WHERE c.embedded = 0 ORDER BY c.id LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            (int(row[0]), "\n".join(part for part in (row[1], row[2], row[3]) if part))
            for row in rows
        ]

    def store_embeddings(self, rows: Iterable[tuple[int, list[float]]]) -> int:
        import sqlite_vec

        count = 0
        for chunk_id, vector in rows:
            self.db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (chunk_id,))
            self.db.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                (chunk_id, sqlite_vec.serialize_float32(vector)),
            )
            self.db.execute("UPDATE chunks SET embedded = 1 WHERE id = ?", (chunk_id,))
            count += 1
        self.db.commit()
        return count

    # --- queries -----------------------------------------------------------

    def keyword(self, match: str, limit: int) -> list[tuple[int, float]]:
        if not match:
            return []
        try:
            rows = self.db.execute(
                "SELECT chunk_id, bm25(chunks_fts, 2.0, 1.5, 1.0) AS score FROM chunks_fts "
                "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
                (match, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(int(row[0]), float(row[1])) for row in rows]

    def nearest(self, vector: list[float], limit: int) -> list[tuple[int, float]]:
        if not self._has_vectors():
            return []
        import sqlite_vec

        rows = self.db.execute(
            "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (sqlite_vec.serialize_float32(vector), limit),
        ).fetchall()
        return [(int(row[0]), float(row[1])) for row in rows]

    def chunks(self, ids: Sequence[int]) -> dict[int, ChunkRow]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.db.execute(
            "SELECT c.id, c.doc_key, c.ordinal, c.heading, c.text, c.line_start, c.line_end, "
            "d.title, d.kind, d.doc_type, d.md_path, d.card_path "
            f"FROM chunks c JOIN docs d USING(doc_key) WHERE c.id IN ({marks})",
            list(ids),
        ).fetchall()
        return {int(row[0]): ChunkRow(*row) for row in rows}

    def stats(self) -> dict[str, int]:
        docs = self.db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        embedded = self.db.execute("SELECT COUNT(*) FROM chunks WHERE embedded = 1").fetchone()[0]
        return {"docs": int(docs), "chunks": int(chunks), "embedded": int(embedded)}
