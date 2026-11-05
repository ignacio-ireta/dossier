"""Run reports: one JSON receipt per command run, content-free by construction.

A report records which stages ran, how long they took, counts, and error codes.
It never records document text, transcript text or model output, so reports can
be shared for troubleshooting without exposing workspace content.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from dossier.core.errors import DossierError
from dossier.core.io import atomic_write_json

CountValue = int | float


class StageStatus(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


@dataclass
class StageRecord:
    name: str
    status: StageStatus = StageStatus.OK
    seconds: float = 0.0
    counts: dict[str, CountValue] = field(default_factory=dict)
    error_code: str | None = None
    note: str = ""


@dataclass
class RunReport:
    command: str
    run_id: str = field(default_factory=new_run_id)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str | None = None
    stages: list[StageRecord] = field(default_factory=list)
    exit_status: int = 0

    @contextmanager
    def stage(self, name: str) -> Iterator[StageRecord]:
        record = StageRecord(name=name)
        self.stages.append(record)
        start = time.monotonic()
        try:
            yield record
        except DossierError as exc:
            record.status = StageStatus.FAILED
            record.error_code = exc.code
            raise
        except Exception:
            record.status = StageStatus.FAILED
            record.error_code = "unexpected"
            raise
        finally:
            record.seconds = round(time.monotonic() - start, 3)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "command": self.command,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_status": self.exit_status,
            "stages": [
                {
                    "name": stage.name,
                    "status": stage.status.value,
                    "seconds": stage.seconds,
                    "counts": stage.counts,
                    "error_code": stage.error_code,
                    "note": stage.note,
                }
                for stage in self.stages
            ],
        }

    def write(self, runs_dir: Path, *, exit_status: int | None = None) -> Path:
        if exit_status is not None:
            self.exit_status = exit_status
        self.finished_at = datetime.now(UTC).isoformat()
        path = runs_dir / self.run_id / "report.json"
        atomic_write_json(path, self.as_dict())
        return path
