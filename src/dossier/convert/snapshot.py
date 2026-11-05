"""Per-source snapshot: the last converted state of every tracked file.

It is the diff baseline for the next run and the change signal for the index
stage (``output_sha256``). It is rewritten atomically and only ever records
completed work, so an interrupted run converges on the next one. A corrupt
snapshot is treated as empty: the source is reconverted, nothing is deleted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from dossier.core.io import atomic_write_text
from dossier.core.paths import WorkspacePaths

log = logging.getLogger("dossier.convert")


class FileState(BaseModel):
    sha256: str
    size: int
    mtime_ns: int
    extractor: str
    status: Literal["ok", "failed"]
    output: str | None = None  # corpus-relative path of the Markdown file
    output_sha256: str | None = None
    extra_outputs: list[str] = Field(default_factory=list)  # meta + CSVs, corpus-relative
    title: str | None = None
    error_code: str | None = None
    run_id: str = ""


class Snapshot(BaseModel):
    schema_version: Literal[1] = 1
    source: str
    root: str
    run_id: str = ""
    updated_at: str = ""
    files: dict[str, FileState] = Field(default_factory=dict)


def snapshot_path(paths: WorkspacePaths, source: str) -> Path:
    return paths.state / "state" / "convert" / f"{source}.json"


def load_snapshot(paths: WorkspacePaths, source: str, root: Path) -> Snapshot:
    path = snapshot_path(paths, source)
    if path.is_file():
        try:
            return Snapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("snapshot for source %s is unreadable; rebuilding", source)
    return Snapshot(source=source, root=str(root))


def save_snapshot(paths: WorkspacePaths, snapshot: Snapshot, run_id: str) -> None:
    snapshot.run_id = run_id
    snapshot.updated_at = datetime.now(UTC).isoformat()
    atomic_write_text(
        snapshot_path(paths, snapshot.source), snapshot.model_dump_json(indent=1) + "\n"
    )
