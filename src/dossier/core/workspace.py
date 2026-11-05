"""A workspace: its paths plus its validated configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dossier.core.config import WorkspaceConfig, load_config
from dossier.core.paths import WorkspacePaths, find_workspace


@dataclass(frozen=True)
class Workspace:
    paths: WorkspacePaths
    config: WorkspaceConfig

    @property
    def root(self) -> Path:
        return self.paths.root


def open_workspace(path: Path | None = None) -> Workspace:
    """Open the workspace at ``path`` (or found from the current directory)."""
    paths = find_workspace(path)
    return Workspace(paths=paths, config=load_config(paths.config))
