"""Helpers shared by every command module: state, consoles, JSON output."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import typer
from rich.console import Console

from dossier.core.errors import WorkspaceNotFoundError
from dossier.core.workspace import Workspace, open_workspace

console = Console()
err_console = Console(stderr=True)


@dataclass
class State:
    workspace_path: Path | None = None
    json: bool = False

    def workspace(self) -> Workspace:
        return open_workspace(self.workspace_path)

    def maybe_workspace(self) -> Workspace | None:
        try:
            return self.workspace()
        except WorkspaceNotFoundError:
            return None


def get_state(ctx: typer.Context) -> State:
    obj = ctx.find_root().obj
    assert isinstance(obj, State)
    return obj


# Set by the entry point in --json mode: the real stdout, kept apart from anything
# libraries print while a command runs (those writes are sent to stderr).
json_stream: TextIO | None = None


def emit(state: State, data: Any) -> None:
    if state.json:
        stream = json_stream or sys.stdout
        stream.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")
        stream.flush()
