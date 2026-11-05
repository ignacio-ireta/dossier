"""Optional qmd backend (https://github.com/tobi/qmd).

``dsr qmd sync`` registers each source's converted Markdown as a qmd collection
in a workspace-specific qmd index (``qmd --index <name>``, so a user's own qmd
collections are untouched), pushes every folder summary as a qmd path context,
then runs ``qmd update`` and ``qmd embed``. ``dsr search --backend qmd`` runs
``qmd query --json`` and maps its results back to workspace documents.

qmd installs with Bun (``bun install -g @tobilu/qmd``) and downloads its own
models into its cache. Its JSON result fields are not formally documented, so
parsing is tolerant: it looks for the file path under several names.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dossier.core.errors import ExternalToolError
from dossier.core.io import sha256_text
from dossier.core.workspace import Workspace
from dossier.index.pipeline import load_manifest
from dossier.index.search import Hit
from dossier.ops.tools import tool

INSTALL_HINT = "bun install -g @tobilu/qmd   (bun: brew install bun)"
MEETINGS = "meetings"  # meetings get their own qmd collection
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? @@[^\n]*\n?")

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=3600)


@dataclass
class Qmd:
    workspace: Workspace
    runner: Runner = _default_runner
    executable: str | None = None

    def __post_init__(self) -> None:
        if self.executable is None:
            located = tool("qmd").locate()
            self.executable = str(located) if located else None

    @property
    def index_name(self) -> str:
        configured = self.workspace.config.index.qmd_index
        if configured:
            return configured
        slug = re.sub(r"[^a-z0-9]+", "-", self.workspace.config.workspace.name.lower()).strip("-")
        return f"dossier-{slug or 'workspace'}-{sha256_text(str(self.workspace.root))[:6]}"

    def collection(self, source: str) -> str:
        # A double dash keeps the meetings collection apart from a source named "meetings".
        return (
            f"{self.index_name}--{source}" if source == MEETINGS else f"{self.index_name}-{source}"
        )

    def _run(self, *args: str) -> str:
        if not self.executable:
            raise ExternalToolError(f"qmd is not installed; install it with: {INSTALL_HINT}")
        command = [self.executable, "--index", self.index_name, *args]
        completed = self.runner(command)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:300]
            raise ExternalToolError(f"qmd {args[0]} failed ({completed.returncode}): {detail}")
        return completed.stdout

    def sync(self) -> dict[str, int]:
        existing = self._run("collection", "list")
        manifest = load_manifest(self.workspace.paths)
        collections = contexts = 0
        for source in self.workspace.config.sources:
            folder = self.workspace.paths.corpus / source.name
            if not folder.is_dir():
                continue
            name = self.collection(source.name)
            if name not in existing:
                self._run("collection", "add", str(folder), "--name", name, "--mask", "**/*.md")
                collections += 1
            root_state = manifest.folders.get(source.name)
            if root_state and root_state.one_line:
                self._run("context", "add", f"qmd://{name}", root_state.one_line)
                contexts += 1
        meetings = self.workspace.paths.meetings
        known = {source.name for source in self.workspace.config.sources}
        if any(meetings.glob("*/summary.md")):
            name = self.collection(MEETINGS)
            if name not in existing:
                self._run("collection", "add", str(meetings), "--name", name, "--mask", "**/*.md")
                collections += 1
            known.add(MEETINGS)
        for key, state in manifest.folders.items():
            source_name, _, sub = key.partition("/")
            if not sub or not state.one_line or source_name not in known:
                continue
            target = f"qmd://{self.collection(source_name)}/{sub}"
            self._run("context", "add", target, state.one_line)
            contexts += 1
        self._run("update")
        self._run("embed")
        return {"collections_added": collections, "contexts": contexts}

    def search(self, query: str, *, limit: int = 10) -> list[Hit]:
        output = self._run("query", query, "--json", "-n", str(limit))
        try:
            data: Any = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ExternalToolError("qmd returned output that is not JSON") from exc
        rows = data.get("results", data) if isinstance(data, dict) else data
        hits: list[Hit] = []
        prefix = f"{self.index_name}-"
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("file") or row.get("path") or row.get("displayPath") or "")
            path = raw.removeprefix("qmd://").split("?", 1)[0].split("#", 1)[0]
            collection, _, relative = path.partition("/")
            source = collection.removeprefix(prefix)
            if source == f"-{MEETINGS}":
                source = MEETINGS
            doc_rel = relative.removesuffix(".md")
            doc = f"{source}/{doc_rel}" if relative else path
            corpus = (
                f"meetings/{relative}"
                if source == MEETINGS
                else f"corpus/{source}/{relative}"
                if relative
                else ""
            )
            snippet = str(row.get("snippet") or "")
            lines = ""
            hunk = _HUNK.match(snippet)
            if hunk:
                start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
                lines = f"{start}-{start + max(count, 1) - 1}"
                snippet = snippet[hunk.end() :]
            elif isinstance(row.get("line"), int):
                lines = f"{row['line']}-{row['line']}"
            hits.append(
                Hit(
                    rank=len(hits) + 1,
                    score=float(row.get("score") or 0.0),
                    doc=doc,
                    title=str(row.get("title") or doc_rel or path),
                    kind="document",
                    type=doc_rel.rsplit(".", 1)[-1] if "." in doc_rel else "",
                    heading="",
                    lines=lines,
                    snippet=re.sub(r"\s+", " ", snippet).strip()[:300],
                    corpus=corpus,
                    card=f"index/cards/{doc}.card.md",
                    matched_by="qmd",
                )
            )
        return hits
