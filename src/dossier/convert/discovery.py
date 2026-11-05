"""Walk a source folder and classify what is there.

Skips hidden files and folders, Office lock files, cloud-only placeholders
(unless the source sets ``materialize``), oversized files, and anything the
include/exclude globs leave out. Directory symlinks are not followed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dossier import platform
from dossier.convert.extractors import MEDIA_SUFFIXES, extractor_for
from dossier.core.config import SourceRoot
from dossier.core.paths import canonical_relpath

_TEMP_NAMES = re.compile(r"^(~\$|\.~lock\.|~WRL)|(\.tmp|\.crdownload|\.part)$", re.IGNORECASE)
_JUNK = {"thumbs.db", "desktop.ini", ".ds_store"}

Kind = Literal["document", "media"]


@dataclass(frozen=True)
class Candidate:
    relpath: str
    path: Path
    size: int
    mtime_ns: int
    kind: Kind


@dataclass
class Discovery:
    root: Path
    documents: list[Candidate] = field(default_factory=list)
    media: list[Candidate] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


@lru_cache(maxsize=256)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob (``**`` spans folders, ``*`` does not) to a regex."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE)


def matches(relpath: str, patterns: list[str]) -> bool:
    return any(glob_regex(pattern).match(relpath) for pattern in patterns)


def discover(root: Path, source: SourceRoot, *, max_bytes: int) -> Discovery:
    found = Discovery(root=root)
    placeholders = platform.current().placeholders
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and not (Path(directory) / d).is_symlink()
        )
        for name in sorted(filenames):
            if name.startswith(".") or name.lower() in _JUNK or _TEMP_NAMES.search(name):
                found.skip("hidden_or_temp")
                continue
            path = Path(directory) / name
            relpath = canonical_relpath(path, root)
            if not matches(relpath, source.include) or matches(relpath, source.exclude):
                found.skip("excluded")
                continue
            suffix = path.suffix.lower()
            kind: Kind
            if suffix in MEDIA_SUFFIXES:
                kind = "media"
            elif extractor_for(path) is not None:
                kind = "document"
            else:
                found.skip("unsupported_type")
                continue
            if not source.materialize and placeholders.is_placeholder(path):
                found.skip("cloud_placeholder")
                continue
            try:
                info = path.stat()
            except OSError:
                found.skip("unreadable")
                continue
            if kind == "document" and info.st_size > max_bytes:
                found.skip("too_large")
                continue
            candidate = Candidate(relpath, path, info.st_size, info.st_mtime_ns, kind)
            (found.media if kind == "media" else found.documents).append(candidate)
    return found
