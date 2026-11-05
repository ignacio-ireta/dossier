"""Logging setup.

Rule for every module: log paths, hashes, counts, stage names and error codes,
never document, transcript or model text. Console output goes through rich;
when a workspace is known, a JSON-lines file log is written under
``.dossier/logs/``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from rich.logging import RichHandler

_FILE_HANDLER_NAME = "dossier-file"


class _JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage()[:1000],
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = record.exc_info[0].__name__
        return json.dumps(entry, ensure_ascii=False)


def configure(level: str = "WARNING", *, log_dir: Path | None = None) -> None:
    root = logging.getLogger("dossier")
    root.setLevel(logging.DEBUG)
    root.propagate = False
    if not any(isinstance(handler, RichHandler) for handler in root.handlers):
        console = RichHandler(show_path=False, rich_tracebacks=False, markup=False)
        console.setLevel(level.upper())
        root.addHandler(console)
    else:
        for handler in root.handlers:
            if isinstance(handler, RichHandler):
                handler.setLevel(level.upper())
    if log_dir is not None and not any(h.get_name() == _FILE_HANDLER_NAME for h in root.handlers):
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        file_handler = logging.FileHandler(log_dir / f"dsr-{stamp}.log", encoding="utf-8")
        file_handler.set_name(_FILE_HANDLER_NAME)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(_JsonLines())
        root.addHandler(file_handler)
