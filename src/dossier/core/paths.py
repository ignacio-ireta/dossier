"""Workspace layout, user directories, and canonical relative paths.

A workspace is any folder holding a ``dossier.toml``. Everything dossier writes
for that workspace lives under it; per-user settings (template packs, the
denylist for contributors, model caches) live in the platform's user dirs.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import platformdirs

from dossier.core.errors import WorkspaceNotFoundError

CONFIG_NAME = "dossier.toml"
STATE_NAME = ".dossier"
WORKSPACE_ENV = "DOSSIER_WORKSPACE"
APP_NAME = "dossier"


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def state(self) -> Path:
        return self.root / STATE_NAME

    @property
    def runs(self) -> Path:
        return self.state / "runs"

    @property
    def logs(self) -> Path:
        return self.state / "logs"

    @property
    def locks(self) -> Path:
        return self.state / "locks"

    @property
    def cache(self) -> Path:
        return self.state / "cache"

    @property
    def sources(self) -> Path:
        return self.root / "sources"

    @property
    def corpus(self) -> Path:
        return self.root / "corpus"

    @property
    def index(self) -> Path:
        return self.root / "index"

    @property
    def meetings(self) -> Path:
        return self.root / "meetings"

    @property
    def people(self) -> Path:
        return self.root / "people"

    @property
    def templates(self) -> Path:
        return self.root / "templates"

    @property
    def out(self) -> Path:
        return self.root / "out"

    def skeleton(self) -> list[Path]:
        """Directories ``dsr init`` creates."""
        return [
            self.sources,
            self.templates,
            self.corpus,
            self.index,
            self.meetings,
            self.people,
            self.out,
            self.runs,
            self.logs,
            self.locks,
            self.cache,
        ]

    def resolve(self, value: str | Path) -> Path:
        """Resolve a config path: relative values are relative to the workspace root."""
        path = Path(str(value)).expanduser()
        return path if path.is_absolute() else self.root / path


def find_workspace(start: Path | None = None) -> WorkspacePaths:
    """Locate the workspace: ``$DOSSIER_WORKSPACE``, else walk up from ``start``."""
    env = os.environ.get(WORKSPACE_ENV)
    if env:
        root = Path(env).expanduser().resolve()
        if not (root / CONFIG_NAME).is_file():
            raise WorkspaceNotFoundError(
                f"{WORKSPACE_ENV} points at {root}, which has no {CONFIG_NAME}", path=root
            )
        return WorkspacePaths(root)
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_NAME).is_file():
            return WorkspacePaths(candidate)
    raise WorkspaceNotFoundError(
        f"no {CONFIG_NAME} found in {current} or any parent; run `dsr init` or pass --workspace",
        path=current,
    )


def user_config_dir() -> Path:
    return platformdirs.user_config_path(APP_NAME, appauthor=False)


def user_data_dir() -> Path:
    return platformdirs.user_data_path(APP_NAME, appauthor=False)


def user_cache_dir() -> Path:
    return platformdirs.user_cache_path(APP_NAME, appauthor=False)


def user_templates_dir() -> Path:
    return user_data_dir() / "templates"


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def canonical_relpath(path: Path, root: Path) -> str:
    """The OS-neutral key for a file under a root: POSIX separators, NFC.

    Snapshots and manifests are keyed by this string so state written on one
    OS stays valid on another and macOS's decomposed filenames match.
    """
    relative = path.relative_to(root)
    return nfc(PurePosixPath(*relative.parts).as_posix())


def casefold_key(relpath: str) -> str:
    """Comparison key for case-insensitive filesystems (macOS and Windows defaults)."""
    return nfc(relpath).casefold()
